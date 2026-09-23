"""全市场 DDE 扫描（条件：5日DDE 由负转正）+ 飞书推送。

## 为什么走「代码前缀 × 单个交易日」

个股页那条路（`get_stock_performance`）有两个硬限制：**单次回答最多 100 行**、
一次只问一只票 —— 全市场 5000 多只根本走不通。所以复用日线采集那套
「代码前缀 × 单个交易日」的选股接口（`collect_universe.crawl_prefixes`，结果集给
CSV、上限 1000 行/段）：**一天 13 次调用拿全市场**，完备性由 `matched` 与行数比对校验。

## 口径（2026-09-22 首次实测，2026-09-23 订正）

| 字段 | 扫描路径给的列 | 个股页路径给的列 | 结论 |
| --- | --- | --- | --- |
| 5 日 DDE | `区间dde大单净额[0917-0923]` | `5日DDE（单位：元）` | **同一个数**：窗口对齐时 688008 两边都是 -119063176.43 |
| **单日** DDE | `dde大单净额[0923]` | `主力净流入额（单位：元）` | **同一个数**：688008 两边都是 -402019797.66 → 落 `net_inflow` 列 |

⚠️ 两条路径的**窗口口径不同，对比前必须先对齐日期**：扫描路径**永远以「今天」为末日**
（问哪一天都一样），个股页按请求日期给窗口。2026-09-23 就栽过一次 —— 拿扫描路径的
`[0917-0923]` 去比「个股页问 09-22」拿到的值，得出「-1.19亿 vs +10.94亿、对不上」的
假结论，白记了一条「待查」。

⚠️ 8.43/8.44 当年把「主力净流入额」映射到了 `主力资金流向[日期]` 那一列，于是得出
「差 380 多倍、单位不明」的结论、**放弃了这一列**。那是**映射错了字段**：正确的对应关系
是 `dde大单净额[单日]` ↔ 个股页的「主力净流入额」（上面那行实测）。2026-09-23 起按正确
字段落库，`net_inflow` 不再是空的。

⚠️ 取单日那列不能用关键词顺序 —— **`dde大单净额` 是 `区间dde大单净额` 的子串**，
必须显式排除带「区间」的列（见 `_pick_dde_day`）。

⚠️ 问法也有讲究：老问法「主力净流入额 与 5日DDE」只回来 **2331 只**，而「当日DDE 与
5日DDE」回来 **5556 只**（全市场）—— 换问法把覆盖率和调用次数都改善了一遍（见 8.53）。

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
from app.db import session_scope, upsert_fill, upsert_many
from app.jobs.collect_universe import crawl_prefixes
from app.jobs.push_brief import already_pushed, send_elements
from app.models import CollectLog, StockBasic, StockDaily, StockDde, TradeCalendar
from app.sources.ifind import IfindClient
from app.sources.markdown_table import pick_float, pick_text, to_float

logger = logging.getLogger(__name__)

# 采集日志里的任务名。去重（`already_pushed`）与记录都用它
DDE_SCAN_TASK = "dde_scan"

_WEEKDAY = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _query(prefix: str, day: date) -> str:
    """问法里必须写清**哪一天** —— 不写日期时选股接口会按「当前」理解（与日线采集同一个坑）。

    问「当日DDE 与 5日DDE」会同时回两列：`dde大单净额[0923]`（单日）与
    `区间dde大单净额[0917-0923]`（5 日）—— 一次调用拿两个口径，不用问两遍。
    """
    return f"证券代码以{prefix}开头的A股股票 {day:%Y%m%d} 的 当日DDE 与 5日DDE"


def _pick_dde_day(item: dict[str, str]) -> float | None:
    """取**单日** DDE（= 个股页的「主力净流入额」，单位元）。

    不能用 `pick(item, "dde大单净额")`：**`dde大单净额` 是 `区间dde大单净额` 的子串**，
    关键词匹配会优先撞上 5 日那列。这里显式要求列名里**不含**「区间」。
    """
    for column, value in item.items():
        if "dde大单净额" in column and "区间" not in column:
            return to_float(value)
    return None


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
    named: list[dict] = []
    for code, item in raw.items():
        # 列名形如 `区间dde大单净额[20260916-20260922]` / `dde大单净额[20260923]`
        dde5 = pick_float(item, "区间dde", "区间DDE")
        dde1 = _pick_dde_day(item)
        # 名字是**同一次调用**带回来的（列名「股票简称」），不额外花钱。顺手存进
        # `stock_basic`：这条路径覆盖全市场，而 `stock_daily` 只有流动性池 + 池外
        # 涨停股 —— 池外那一千来只在日线里查不到名字，推送与 DDE 榜上就只剩代码
        name = pick_text(item, "股票简称", "证券简称", "简称")
        if name:
            named.append({"code": code, "name": name})
        if dde5 is None and dde1 is None:
            continue
        # 单日 DDE 落 `net_inflow`（实测它就是个股页的「主力净流入额」，同一个数）；
        # `upsert_fill` 保证某一边缺值时不会把已有的另一边抹成 NULL
        rows.append({"trade_date": day, "code": code, "dde": dde5, "net_inflow": dde1})

    with session_scope() as session:
        written = upsert_fill(session, StockDde, rows)
        upsert_many(session, StockBasic, named)
    logger.info(
        "DDE 全市场抓取 %s：落库 %d 只 / 名称 %d 条（%d 次调用）",
        day,
        written,
        len(named),
        calls,
    )
    return written, calls


def _names(session, codes: set[str]) -> dict[str, str | None]:
    """命中票的名字。

    先查 `stock_basic` —— DDE 抓取每天把全市场简称写进去（见 `StockBasic` 的说明），
    它是一张小表、一次读完就行；库里还没有的再退回 `stock_daily`。

    顺序是刻意的：`stock_daily` 那条路**没有 trade_date 过滤**（名字是代码的属性，
    不是某一天的属性），于是每个代码要扫过它全部历史行，几百个代码就是几十万行 ——
    拿它当主路径会让每次推送都白扫一遍。
    """
    if not codes:
        return {}
    named: dict[str, str | None] = dict(
        session.execute(select(StockBasic.code, StockBasic.name)).all()  # type: ignore[arg-type]
    )
    missing = {code for code in codes if not named.get(code)}
    if missing:
        for code, name in session.execute(
            select(StockDaily.code, StockDaily.name).where(
                StockDaily.code.in_(missing), StockDaily.name.is_not(None)
            )
        ).all():
            named.setdefault(code, name)
    return {code: named.get(code) for code in codes}


def _prev_trade_date(session, day: date) -> date | None:
    """`day` 之前最近的一个交易日。"""
    return session.scalar(
        select(TradeCalendar.trade_date)
        .where(TradeCalendar.trade_date < day)
        .order_by(TradeCalendar.trade_date.desc())
        .limit(1)
    )


# 推送里两张榜各取前几名。**10 是表格组件的上限**（`page_size` 最多 10 行），
# 再多就得在卡片里翻页 —— 而榜本来就是「先看头部」
BOARD_LIMIT = 10

# 「转正」清单在推送里最多列这么多只：覆盖面扩到全市场之后它能到 150+ 只，
# 全列出来会把上面的表格淹掉，其余让人去页面看
TURN_LIST_CAP = 30


def rankings(day: date, *, limit: int = BOARD_LIMIT) -> dict[str, list[dict]]:
    """当日两张榜：`dde5`（5日DDE）与 `dde1`（单日主力净流入），各取**流入前** limit 名。

    对比值取**前一个交易日的同一口径**；前一日没有数据时给 None —— 不拿 0 顶
    （「没有数据」与「是 0」是两件事），符号列那时显示「—」。
    """
    with session_scope() as session:
        prev = _prev_trade_date(session, day)
        today = {
            row.code: row
            for row in session.scalars(select(StockDde).where(StockDde.trade_date == day))
        }
        before = (
            {
                row.code: row
                for row in session.scalars(
                    select(StockDde).where(StockDde.trade_date == prev)
                )
            }
            if prev is not None
            else {}
        )
        names = _names(session, set(today))

    boards: dict[str, list[dict]] = {}
    for key, column in (("dde5", "dde"), ("dde1", "net_inflow")):
        rows: list[dict] = []
        for code, row in today.items():
            value = getattr(row, column)
            if value is None:
                continue
            old = before.get(code)
            prev_value = getattr(old, column) if old is not None else None
            if prev_value is None:
                sign = "—"
            elif value > prev_value:
                sign = "↑"
            elif value < prev_value:
                sign = "↓"
            else:
                sign = "="
            rows.append(
                {
                    "code": code,
                    "name": names.get(code),
                    "value": value,
                    "prev": prev_value,
                    "sign": sign,
                }
            )
        rows.sort(key=lambda item: item["value"], reverse=True)
        boards[key] = rows[:limit]
    return boards


def scan(day: date) -> list[dict]:
    """找出 5日DDE 由负转正的票，按当日 DDE 降序。"""
    with session_scope() as session:
        prev = _prev_trade_date(session, day)
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


def _board_table(title: str, value_label: str, rows: list[dict]) -> list[dict]:
    """一张榜 = 标题块 + 表格，两个元素。

    表格组件**只能挂在卡片根节点**（不能塞进 div 里），所以这里返回的是元素列表，
    由 `build_card` 拼进 `elements`，而不是一整个字符串。

    单元格里放的是**格式化好的文本**（`+3.42亿`）而不是数字列：数字列要自己换单位，
    而「亿/万」这种写法一眼就读得出来。
    """
    return [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**{title}**"}},
        {
            "tag": "table",
            "page_size": BOARD_LIMIT,
            "row_height": "low",
            "columns": [
                {"name": "name", "display_name": "名称", "data_type": "text", "width": "auto"},
                {"name": "code", "display_name": "代码", "data_type": "text"},
                {
                    "name": "value",
                    "display_name": value_label,
                    "data_type": "text",
                    "horizontal_align": "right",
                },
                {
                    "name": "prev",
                    "display_name": "前一日",
                    "data_type": "text",
                    "horizontal_align": "right",
                },
                {"name": "sign", "display_name": "较前日", "data_type": "text"},
            ],
            "rows": [
                {
                    "name": row["name"] or row["code"],
                    "code": row["code"],
                    "value": _amount(row["value"]),
                    "prev": _amount(row["prev"]),
                    "sign": row["sign"],
                }
                for row in rows
            ],
        },
    ]


def build_card(day: date, hits: list[dict], boards: dict[str, list[dict]]) -> list[dict]:
    """飞书卡片元素：摘要 + 两张榜（表格）+ 转正清单 + 口径脚注。

    标题里的「收盘复盘」四个字是**刻意的**：群机器人多配了自定义关键词校验，
    消息里必须出现那个词才发得出去。
    """
    shown = hits[:TURN_LIST_CAP]
    lines = [
        f"- **{item['name'] or item['code']}** {item['code']} · 5日DDE {_amount(item['dde'])}"
        for item in shown
    ]
    if len(hits) > len(shown):
        rest = len(hits) - len(shown)
        lines.append(f"- …另有 **{rest} 只**，去页面「资金面 → DDE 排名」看全量")
    if not lines:
        lines.append("- 今日无")
    return [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**收盘复盘 · DDE 排名 · {day.isoformat()}（{_WEEKDAY[day.weekday()]}）**\n"
                    f"5日DDE 由负转正 **{len(hits)} 只**（前一日 ≤ 0、当日 > 0）"
                ),
            },
        },
        *_board_table("5 日 DDE · 流入前 10", "5日DDE", boards["dde5"]),
        *_board_table("单日主力净流入 · 前 10", "单日", boards["dde1"]),
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**转正清单**（按当日 5日DDE 降序）\n" + "\n".join(lines),
            },
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    "> 口径都是 iFinD、单位元。**单日** = `dde大单净额[当日]`，实测与个股页"
                    "「主力净流入额」是同一个数；**5日DDE** = `区间dde大单净额`（近 5 个交易日）。\n"
                    "> 「较前日」是跟前一个交易日的同一口径比。只推信号、不含建议。"
                ),
            },
        },
    ]


def build_markdown(day: date, hits: list[dict]) -> str:
    """纯文字版正文 —— 只在没有 webhook、退回 lark-cli 时用（那里发不了卡片元素）。"""
    lines = [
        f"**收盘复盘 · 5日DDE 转正 · {day.isoformat()}（{_WEEKDAY[day.weekday()]}）**",
        "",
        f"共 **{len(hits)} 只**：前一日 5日DDE ≤ 0、当日 > 0（按当日 DDE 降序）",
        "",
    ]
    for item in hits:
        # 没名字时**只打印一次代码**，别印两遍：池外的票（每天一千来只）名字要等
        # `collect_market` 存进 `stock_basic` 才有，历史上推出来每行都是
        # 「688084 688084 · 5日DDE …」，看着像坏了
        label = (
            f"**{item['name']}** {item['code']}"
            if item["name"]
            else f"**{item['code']}**"
        )
        parts = [label, f"5日DDE {_amount(item['dde'])}"]
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


def push(
    day: date, hits: list[dict], boards: dict[str, list[dict]], settings: Settings
) -> dict:
    """推送。**空结果不发** —— 与形态推送同一取舍：发一条「今天没有」只是噪音。

    空结果也写一条 `status="empty"` 的记录（在「数据管理」页看得到），
    但它**不算「已推送」**：`already_pushed` 只认 `ok`，免得中午手工跑一次空的，
    把收盘后那次真推送挡掉。

    发的是**卡片元素**（两张榜是表格），没配 webhook 时退回纯文字版。
    """
    if already_pushed(day, DDE_SCAN_TASK):
        return {"status": "skipped", "reason": "今天已经推过"}
    if not hits and not any(boards.values()):
        _record(day, "empty", 0, "无命中，未推送")
        return {"status": "empty"}

    result = send_elements(
        build_card(day, hits, boards),
        settings,
        fallback_markdown=build_markdown(day, hits),
    )
    if result.get("ok"):
        _record(day, "ok", len(hits), f"推送成功（转正 {len(hits)} 只 + 两张榜）")
        return {"status": "ok", "rows": len(hits)}
    _record(day, "failed", 0, str(result.get("error")))
    return {"status": "failed", "reason": result.get("error")}


def run(day: date, settings: Settings | None = None) -> dict:
    """抓取 → 扫描（转正）→ 两张榜 → 推送。返回一份给日志看的摘要。"""
    settings = settings or get_settings()
    written, calls = collect_market(day)
    hits = scan(day)
    boards = rankings(day)
    result = push(day, hits, boards, settings)
    logger.info(
        "DDE 扫描完成 %s：落库 %d 只（%d 次调用）→ 转正 %d 只 → 榜 %d/%d 条 → 推送 %s",
        day,
        written,
        calls,
        len(hits),
        len(boards["dde5"]),
        len(boards["dde1"]),
        result.get("status"),
    )
    return {
        "trade_date": str(day),
        "stored": written,
        "calls": calls,
        "hits": len(hits),
        "boards": {key: len(rows) for key, rows in boards.items()},
        "push": result.get("status"),
    }
