"""全市场 DDE 扫描（条件：5日DDE 由负转正）+ 飞书推送。

## 为什么走「代码前缀 × 单个交易日」

个股页那条路（`get_stock_performance`）有两个硬限制：**单次回答最多 100 行**、
一次只问一只票 —— 全市场 5000 多只根本走不通。所以复用日线采集那套
「代码前缀 × 单个交易日」的选股接口（`collect_universe.crawl_prefixes`，结果集给
CSV、上限 1000 行/段）：**一天 13 次调用拿全市场**，完备性由 `matched` 与行数比对校验。

## 口径（2026-09-22 交叉实测）

| 字段 | 扫描路径给的列 | 个股页路径给的列 | 结论 |
| --- | --- | --- | --- |
| DDE | `区间dde大单净额[0916-0922]` = -25657960.72 | `5日DDE` = -25657960.72 | 同一天**完全一致** → 写同一张 `stock_dde` |
| 主力净流入 | `主力资金流向[0922]` = 30963.65 | `主力净流入额` = 11860651 | **对不上**（差 380 多倍、单位不明）→ **不写** |

所以这里只落 `dde` 列，`net_inflow` 一律不传 —— `upsert_fill` 保证传 None 不会把
个股页已经补上的值覆盖掉。

⚠️ 但这条路径的 DDE **与请求日期无关、永远是「最新」**（实测同一天问 09-21 与 09-22，
1336 只里 1334 只数值完全相同）→ **只能逐日累积、不能回补历史**，且**只允许抓最近
交易日**（见 `collect_market` 的守卫）。

## 条件：5日DDE 由负转正

前一个交易日 ≤ 0、当日 > 0。两个交易日**都必须有数据**才判 —— 缺一天就不算转正：
DDE 是区间口径，把缺失当 0 会造出假信号，宁可漏报也不误报。

**这个条件要从启用后的第二个交易日才可能有结果**（前一天没有累积值就不判）。
"""

import logging
from datetime import date

from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import session_scope, upsert_fill
from app.jobs.collect_universe import crawl_prefixes
from app.jobs.push_brief import already_pushed, send_markdown
from app.models import CollectLog, StockBasic, StockDaily, StockDde, TradeCalendar
from app.sources.ifind import IfindClient
from app.sources.markdown_table import pick_float

logger = logging.getLogger(__name__)

# 采集日志里的任务名。去重（`already_pushed`）与记录都用它
DDE_SCAN_TASK = "dde_scan"

_WEEKDAY = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _query(prefix: str, day: date) -> str:
    """问法里必须写清**哪一天** —— 不写日期时选股接口会按「当前」理解（与日线采集同一个坑）。"""
    return (
        f"证券代码以{prefix}开头的A股股票 {day:%Y%m%d} 的 主力净流入额 与 5日DDE"
    )


def _amount(value: float | None) -> str:
    """元 → 带符号的万/亿。前端有 `fmtAmount`，但这是飞书正文，得在服务端格式化。"""
    if value is None:
        return "—"
    if abs(value) >= 1e8:
        return f"{value / 1e8:+.2f}亿"
    return f"{value / 1e4:+.2f}万"


def _latest_trade_date() -> date | None:
    with session_scope() as session:
        return session.scalar(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date <= date.today())
            .order_by(TradeCalendar.trade_date.desc())
            .limit(1)
        )


def collect_market(day: date) -> tuple[int, int]:
    """抓全市场当天的 DDE 并落库。返回（写入行数, 调用次数）。

    ⚠️ **只允许抓最近交易日**。实测这条路径的 DDE **与请求日期无关、永远是「最新」**：
    同一天分别问 09-21 与 09-22，返回的数值 1336 只里有 1334 只完全相同（而个股页
    那条路对同一只票逐日是变化的）。所以对历史日期抓，等于把今天的值写到过去 ——
    与 `jobs/collect_daily._require_today` 防的是同一类静默污染。

    推论：**它只能逐日累积**（今天抓到的就是今天的值），历史补不回来。
    「由负转正」这个条件因此也要等积累出两个交易日才判得出来。
    """
    latest = _latest_trade_date()
    if latest is None or day != latest:
        logger.warning(
            "DDE 全市场抓取只支持最近交易日（请求 %s，最近 %s），本次跳过",
            day,
            latest,
        )
        return 0, 0

    raw, calls = crawl_prefixes(
        IfindClient(), lambda prefix: _query(prefix, day), str(day)
    )
    rows: list[dict] = []
    for code, item in raw.items():
        # 列名形如 `区间dde大单净额[20260916-20260922]`，用关键词包含匹配取值
        value = pick_float(item, "区间dde", "dde", "DDE")
        if value is None:
            continue
        rows.append({"trade_date": day, "code": code, "dde": value})

    with session_scope() as session:
        written = upsert_fill(session, StockDde, rows)
    logger.info("DDE 全市场抓取 %s：落库 %d 只（%d 次调用）", day, written, calls)
    return written, calls


def _names(session, codes: set[str]) -> dict[str, str | None]:
    """命中票的名字。`stock_dde` 不存名字，从日线里取，取不到再退回基础信息表。"""
    if not codes:
        return {}
    names: dict[str, str | None] = dict(
        session.execute(
            select(StockDaily.code, StockDaily.name).where(
                StockDaily.code.in_(codes), StockDaily.name.is_not(None)
            )
        ).all()  # type: ignore[arg-type]
    )
    missing = codes - set(names)
    if missing:
        names.update(
            dict(
                session.execute(
                    select(StockBasic.code, StockBasic.name).where(
                        StockBasic.code.in_(missing)
                    )
                ).all()  # type: ignore[arg-type]
            )
        )
    return names


def scan(day: date) -> list[dict]:
    """找出 5日DDE 由负转正的票，按当日 DDE 降序。"""
    with session_scope() as session:
        prev = session.scalar(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date < day)
            .order_by(TradeCalendar.trade_date.desc())
            .limit(1)
        )
        if prev is None:
            logger.warning("交易日历里 %s 之前没有交易日，跳过扫描", day)
            return []
        today = {
            row.code: row
            for row in session.scalars(
                select(StockDde).where(StockDde.trade_date == day)
            )
        }
        before = {
            row.code: row
            for row in session.scalars(
                select(StockDde).where(StockDde.trade_date == prev)
            )
        }
        names = _names(session, set(today))

    hits: list[dict] = []
    for code, row in today.items():
        if row.dde is None or row.dde <= 0:
            continue
        old = before.get(code)
        # 前一日必须有数据、且 ≤ 0，才算「转正」
        if old is None or old.dde is None or old.dde > 0:
            continue
        hits.append(
            {
                "code": code,
                "name": names.get(code),
                "dde": row.dde,
                "prev_dde": old.dde,
                "net_inflow": row.net_inflow,
            }
        )
    hits.sort(key=lambda item: item["dde"], reverse=True)
    logger.info("DDE 扫描 %s：当日 %d 只有数据，命中 %d 只", day, len(today), len(hits))
    return hits


def build_markdown(day: date, hits: list[dict]) -> str:
    """飞书正文。

    标题带「收盘复盘」四个字是**刻意的**：群自定义机器人多半配了自定义关键词校验，
    而消息里必须出现那个词才发得出去（`push_brief` 的标题同理）。
    """
    lines = [
        f"**收盘复盘 · 5日DDE 转正 · {day.isoformat()}（{_WEEKDAY[day.weekday()]}）**",
        "",
        f"共 **{len(hits)} 只**：前一日 5日DDE ≤ 0、当日 > 0（按当日 DDE 降序）",
        "",
    ]
    for item in hits:
        parts = [
            f"**{item['name'] or item['code']}** {item['code']}",
            f"5日DDE {_amount(item['dde'])}",
        ]
        if item["net_inflow"] is not None:
            parts.append(f"净流入 {_amount(item['net_inflow'])}")
        lines.append("- " + " · ".join(parts))
    lines += [
        "",
        "> 口径：iFinD `区间dde大单净额`，与个股页的「5日DDE」是同一个数（实测一致）。",
        "> 只推信号、不含建议。",
    ]
    return "\n".join(lines)


def _record(day: date, status: str, rows: int, message: str) -> None:
    with session_scope() as session:
        session.add(
            CollectLog(
                trade_date=day,
                task=DDE_SCAN_TASK,
                status=status,
                rows=rows,
                message=message,
            )
        )


def push(day: date, hits: list[dict], settings: Settings) -> dict:
    """推送。**空结果不发** —— 与形态推送同一取舍：发一条「今天没有」只是噪音。

    空结果也写一条 `status="empty"` 的记录（在「数据管理」页看得到），
    但它**不算「已推送」**：`already_pushed` 只认 `ok`，免得中午手工跑一次空的，
    把收盘后那次真推送挡掉。
    """
    if already_pushed(day, DDE_SCAN_TASK):
        return {"status": "skipped", "reason": "今天已经推过"}
    if not hits:
        _record(day, "empty", 0, "无命中，未推送")
        return {"status": "empty"}

    result = send_markdown(build_markdown(day, hits), settings)
    if result.get("ok"):
        _record(day, "ok", len(hits), f"推送成功（{len(hits)} 只）")
        return {"status": "ok", "rows": len(hits)}
    _record(day, "failed", 0, str(result.get("error")))
    return {"status": "failed", "reason": result.get("error")}


def run(day: date, settings: Settings | None = None) -> dict:
    """抓取 → 扫描 → 推送。返回一份给日志看的摘要。"""
    settings = settings or get_settings()
    written, calls = collect_market(day)
    hits = scan(day)
    result = push(day, hits, settings)
    logger.info(
        "DDE 扫描完成 %s：落库 %d 只（%d 次调用）→ 命中 %d 只 → 推送 %s",
        day,
        written,
        calls,
        len(hits),
        result.get("status"),
    )
    return {
        "trade_date": str(day),
        "stored": written,
        "calls": calls,
        "hits": len(hits),
        "push": result.get("status"),
    }
