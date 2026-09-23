"""板块资金流：用**开盘啦的板块成分股** × 我们自己算的**逐股主力净流入**求和。

## 为什么自己算

开盘啦的板块行里**有**像净流入的列（`[6][7][8][12]`），但当年实测过：拿成分股
逐只求和各列都比不上、口径不明（见 `sources/kaipanhong.py` 顶部那段列说明）。
「取一个连自己都解释不了的数」比「自己算一个能解释的」差得多，所以：

    板块净流入 = Σ(该板块成分股的当日主力净流入)

成分股名单来自开盘啦（`board_members`），逐股净流入来自 `stock_dde.net_inflow`
（= 个股页那一栏「主力净流入额」，2026-09-23 实测同源，见 8.53）。

⚠️ **这是本项目自己定义的口径**，与开盘啦 App 里它的板块资金流**不保证相等**
（那是它自研口径）。页面上照这个写文案，别写成「开盘啦的资金流」。

## 成本与前置

- **零 iFinD 配额**：走开盘啦成分股接口，精选 270 + 行业 104 = 374 个板块，
  3/s 限速下约 2~3 分钟 —— 所以它在采集链的最末尾。
- **依赖 `stock_dde`**：那一步被配额让路跳过的话，这里就没有逐股净流入可加。
  所以下面有 `MIN_COVERAGE` 那道守卫：**宁可不写，也不写一堆 0**。
- 成分股明细**不落库**（374 个板块几万行/天，纯属浪费），只落聚合结果；
  页面上「点开看成分股」仍走原来那套按需抓取。

## 当日成分股可能还没更新

开盘啦的成分股**当日要等盘后某个时刻**才更新（实测 21:20 还没有、21:55 有了，
不是固定时刻，见 `board_members` 的说明）。17:30 采集时大概率拿到的还是**上一交易日**
的名单 —— 成分股变动很慢、差一天影响有限，所以这里**自动整轮退回上一交易日**并记日志，
而不是让 374 个板块各失败一次（日志刷屏、还白等几分钟）。
"""

import logging
import time
from datetime import date

from sqlalchemy import func, select

from app.config import Settings, get_settings
from app.db import session_scope, upsert_fill
from app.models import CollectLog, SectorDaily, StockDde, TradeCalendar
from app.sources.kaipanhong import KaipanhongSource

logger = logging.getLogger(__name__)

# 采集日志里的任务名
BOARD_FLOW_TASK = "board_flow"

# 逐股净流入的覆盖度下限（只数）。低于它就认定 `stock_dde` 那一步没跑成 ——
# 全市场正常是 5000+ 只，卡 1000 只留足余量
MIN_COVERAGE = 1000


def _prev_trade_date(day: date) -> date | None:
    with session_scope() as session:
        return session.scalar(
            select(func.max(TradeCalendar.trade_date)).where(
                TradeCalendar.trade_date < day
            )
        )


def _flow_map(day: date) -> dict[str, float]:
    """当日逐股主力净流入（元）。

    **只取有值的**：缺数据的票不能当 0 参与求和 —— 那会把板块净流入算小，
    而且看不出来（同 `sentiment` 里「不拿 0 顶替缺失」的取舍）。
    """
    with session_scope() as session:
        rows = session.execute(
            select(StockDde.code, StockDde.net_inflow).where(
                StockDde.trade_date == day, StockDde.net_inflow.is_not(None)
            )
        ).all()
    return {str(code): float(value) for code, value in rows}


def _boards(day: date) -> list[tuple[str, str]]:
    """当天有行情的板块（代码, 名称），精选与行业一起。"""
    with session_scope() as session:
        rows = session.execute(
            select(SectorDaily.sector_code, SectorDaily.name).where(
                SectorDaily.trade_date == day
            )
        ).all()
    return [(str(code), str(name or code)) for code, name in rows]


def _record(day: date, status: str, rows: int, message: str, cost: float | None = None) -> None:
    with session_scope() as session:
        session.add(
            CollectLog(
                trade_date=day,
                task=BOARD_FLOW_TASK,
                status=status,
                rows=rows,
                message=message,
                cost_seconds=cost,
            )
        )


def aggregate(day: date, settings: Settings | None = None) -> dict:
    """算一遍当天的板块净流入，写进 `sector_daily.net_inflow`（只写这一列）。"""
    settings = settings or get_settings()
    started = time.monotonic()

    flows = _flow_map(day)
    if len(flows) < MIN_COVERAGE:
        message = f"只有 {len(flows)} 只票有净流入（< {MIN_COVERAGE}），先看 DDE 那一步"
        logger.warning("板块资金流跳过 %s：%s", day, message)
        _record(day, "skipped", 0, message)
        return {"status": "skipped", "reason": message, "stocks": len(flows)}

    boards = _boards(day)
    if not boards:
        raise RuntimeError(f"{day} 没有板块行情，先跑 collect_sectors")

    source = KaipanhongSource(settings)
    calls = 0

    # 先探一个板块：当日的取不到就是接口整体还没更新（见模块说明），整轮退回上一交易日
    member_day = day
    probe_code = boards[0][0]
    try:
        calls += 1
        source.board_members(probe_code, day)
    except RuntimeError:
        fallback = _prev_trade_date(day)
        if fallback is None:
            raise RuntimeError(f"{day} 的板块成分股取不到，日历里也没有上一交易日") from None
        member_day = fallback
        logger.warning(
            "开盘啦 %s 的板块成分股还没更新，整轮改用 %s 的名单（成分变动很慢，差一天可接受）",
            day,
            member_day,
        )

    rows: list[dict] = []
    failed = 0
    empty = 0
    unmatched: list[str] = []
    for code, name in boards:
        calls += 1
        try:
            members = source.board_members(code, member_day)
        except Exception as exc:  # noqa: BLE001 - 单个板块取不到不该让整轮失败
            failed += 1
            logger.debug("板块 %s %s 成分股取不到：%s", code, name, exc)
            continue

        codes = [str(item["code"]).strip() for item in members if item.get("code")]
        if not codes:
            empty += 1
            continue
        hit = [flow for flow in (flows.get(c) for c in codes) if flow is not None]
        if not hit:
            # 成分股一只都不在净流入表里：名单与行情对不上，记下来别静默
            unmatched.append(f"{code} {name}")
            continue
        rows.append(
            {"trade_date": day, "sector_code": code, "net_inflow": sum(hit)}
        )

    with session_scope() as session:
        written = upsert_fill(session, SectorDaily, rows)

    cost = round(time.monotonic() - started, 2)
    logger.info(
        "板块资金流完成 %s：%d 个板块 → 写 %d 行（名单用 %s / 空成分 %d / 取不到 %d / "
        "对不上 %d / 逐股覆盖 %d 只 / %d 次请求），用时 %ss",
        day,
        len(boards),
        written,
        member_day,
        empty,
        failed,
        len(unmatched),
        len(flows),
        calls,
        cost,
    )
    if unmatched:
        logger.warning("有 %d 个板块的成分股与净流入表完全对不上：%s", len(unmatched), unmatched[:5])
    _record(
        day,
        "ok",
        written,
        f"{len(boards)} 个板块 / 名单 {member_day} / 逐股覆盖 {len(flows)} 只",
        cost,
    )
    return {
        "status": "ok",
        "trade_date": day.isoformat(),
        "member_date": member_day.isoformat(),
        "boards": len(boards),
        "written": written,
        "empty": empty,
        "failed": failed,
        "unmatched": len(unmatched),
        "stocks": len(flows),
        "calls": calls,
        "cost_seconds": cost,
    }
