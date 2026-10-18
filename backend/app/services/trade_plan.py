"""次日计划的读取与自动验证（2026-10-18 清单 #2）。

计划本身只有「过 X 买 / 破 Y 放弃」两个价，**验证全部用日线现算**：
- 触发：计划日**最高价 ≥ buy_price**；
- 放弃：计划日**最低价 ≤ stop_price**；
- 两者同时出现时标注「同日触发并跌破」——日线没有盘中顺序，不猜哪个先发生。
- 触发后走势：计划日收盘、未来 1/3/5 个交易日的收盘相对买点的收益。

「写进简报」只取**管理员**的计划：简报是全局推送，不能把其他会员的计划发到群里。
"""

import logging
from collections import defaultdict
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AppUser,
    PatternHit,
    StockBasic,
    StockDaily,
    TradeCalendar,
    TradePlan,
    Watchlist,
)
from app.services.patterns import PATTERNS

logger = logging.getLogger(__name__)

PATTERN_NAMES = {pattern.key: pattern.name for pattern in PATTERNS}
_KEYS = tuple(PATTERN_NAMES)


def _calendar(session: Session) -> tuple[list[date], dict[date, int]]:
    days = list(
        session.scalars(select(TradeCalendar.trade_date).order_by(TradeCalendar.trade_date))
    )
    return days, {day: index for index, day in enumerate(days)}


def _bars(session: Session, codes: set[str], start: date) -> dict[str, dict[date, tuple]]:
    """`{code: {交易日: (最高, 最低, 收盘)}}`，只取三价齐全的行。"""
    if not codes:
        return {}
    result: dict[str, dict[date, tuple]] = defaultdict(dict)
    rows = session.execute(
        select(
            StockDaily.code,
            StockDaily.trade_date,
            StockDaily.high,
            StockDaily.low,
            StockDaily.close,
        ).where(
            StockDaily.code.in_(codes),
            StockDaily.trade_date >= start,
            StockDaily.high.is_not(None),
            StockDaily.low.is_not(None),
            StockDaily.close.is_not(None),
        )
    ).all()
    for code, day, high, low, close in rows:
        result[code][day] = (float(high), float(low), float(close))
    return result


def _next_returns(
    buy_price: float,
    bars: dict[date, tuple],
    calendar: list[date],
    pos: dict[date, int],
    plan_date: date,
) -> dict:
    out = {}
    start = pos.get(plan_date)
    for horizon in (1, 3, 5):
        value = None
        if start is not None and start + horizon < len(calendar):
            bar = bars.get(calendar[start + horizon])
            if bar is not None:
                value = round((bar[2] / buy_price - 1) * 100, 2)
        out[f"next_{horizon}d_pct"] = value
    return out


def evaluate(
    plan: TradePlan,
    bars: dict[date, tuple],
    calendar: list[date],
    pos: dict[date, int],
    data_end: date | None,
) -> dict:
    """算一条计划的结果。日线还没到 / 停牌时给 `status` 说明，不编造。"""
    blank = {
        "status": "待验证",
        "high": None,
        "low": None,
        "close": None,
        "day_close_pct": None,
        "max_favorable_pct": None,
        "max_adverse_pct": None,
        "next_1d_pct": None,
        "next_3d_pct": None,
        "next_5d_pct": None,
    }
    bar = bars.get(plan.plan_date)
    if bar is None:
        if data_end is not None and plan.plan_date <= data_end:
            return {**blank, "status": "无日线（停牌或未采集）"}
        return blank

    high, low, close = bar
    triggered = high >= plan.buy_price
    broken = low <= plan.stop_price
    if triggered and broken:
        status = "同日触发并跌破"
    elif triggered:
        status = "触发"
    elif broken:
        status = "放弃（破位）"
    else:
        status = "未触发"

    result = {
        "status": status,
        "high": high,
        "low": low,
        "close": close,
        "day_close_pct": None,
        "max_favorable_pct": None,
        "max_adverse_pct": None,
        "next_1d_pct": None,
        "next_3d_pct": None,
        "next_5d_pct": None,
    }
    if triggered:
        result["day_close_pct"] = round((close / plan.buy_price - 1) * 100, 2)
        result["max_favorable_pct"] = round((high / plan.buy_price - 1) * 100, 2)
        result["max_adverse_pct"] = round((low / plan.buy_price - 1) * 100, 2)
        result.update(_next_returns(plan.buy_price, bars, calendar, pos, plan.plan_date))
    return result


def load(session: Session, user_id: int, plan_date: date | None = None) -> list[dict]:
    """该用户的计划 + 现算验证，按计划日倒序。"""
    statement = select(TradePlan).where(TradePlan.user_id == user_id)
    if plan_date is not None:
        statement = statement.where(TradePlan.plan_date == plan_date)
    plans = list(
        session.scalars(statement.order_by(TradePlan.plan_date.desc(), TradePlan.id.desc()))
    )
    if not plans:
        return []

    calendar, pos = _calendar(session)
    data_end = session.scalar(select(func.max(StockDaily.trade_date)))
    bars = _bars(session, {plan.code for plan in plans}, min(plan.plan_date for plan in plans))
    return [
        {
            "id": plan.id,
            "code": plan.code,
            "name": plan.name,
            "plan_date": plan.plan_date,
            "buy_price": plan.buy_price,
            "stop_price": plan.stop_price,
            "note": plan.note,
            **evaluate(plan, bars.get(plan.code, {}), calendar, pos, data_end),
            "created_at": plan.created_at,
            "updated_at": plan.updated_at,
        }
        for plan in plans
    ]


def admin_user_id(session: Session) -> int | None:
    """简报只取管理员的计划（全局推送不能带其他会员的计划）。"""
    return session.scalar(
        select(AppUser.id)
        .where(AppUser.is_admin.is_(True))
        .order_by(AppUser.id)
        .limit(1)
    )


def brief_rows(session: Session, trade_date: date) -> list[dict]:
    user_id = admin_user_id(session)
    if user_id is None:
        return []
    return load(session, user_id, plan_date=trade_date)


def _levels(key_levels: dict | None) -> tuple[float | None, float | None]:
    """从 `key_levels` 里取（买点, 放弃位）。"""
    data = key_levels or {}
    buy = data.get("breakout") or data.get("watch_high")
    stop = data.get("support") or data.get("wash_low") or data.get("start_low")
    return (
        float(buy) if buy else None,
        float(stop) if stop else None,
    )


def suggestions(session: Session, user_id: int) -> list[dict]:
    """次日计划的候选：自选股 + 当日评分最高的形态命中（带出 key_levels）。

    自选股即使今天没有命中也会出现（价格留空，用户自己填）；形态命中只取前 10 只
    不在自选里的，避免把 50 只全部铺出来。
    """
    latest = session.scalar(select(func.max(PatternHit.trade_date)))
    hits = []
    if latest is not None:
        hits = session.execute(
            select(
                PatternHit.code,
                PatternHit.name,
                PatternHit.pattern,
                PatternHit.score,
                PatternHit.key_levels,
            )
            .where(PatternHit.trade_date == latest, PatternHit.pattern.in_(_KEYS))
            .order_by(PatternHit.score.desc())
        ).all()

    best: dict[str, tuple] = {}
    for row in hits:
        best.setdefault(row[0], row)

    watch_codes = list(
        session.scalars(select(Watchlist.code).where(Watchlist.user_id == user_id))
    )
    watch = set(watch_codes)
    names = dict(
        session.execute(
            select(StockBasic.code, StockBasic.name).where(
                StockBasic.code.in_(watch | set(best))
            )
        ).all()
    )

    result: list[dict] = []
    for code in sorted(watch):
        hit = best.get(code)
        buy, stop = _levels(hit[4] if hit else None)
        result.append(
            {
                "code": code,
                "name": (hit[1] if hit else None) or names.get(code),
                "source": "watchlist",
                "in_watchlist": True,
                "pattern": hit[2] if hit else None,
                "pattern_name": PATTERN_NAMES.get(hit[2]) if hit else None,
                "buy_price": buy,
                "stop_price": stop,
            }
        )
    added = 0
    for code, hit in best.items():
        if code in watch:
            continue
        if added >= 10:
            break
        buy, stop = _levels(hit[4])
        result.append(
            {
                "code": code,
                "name": hit[1] or names.get(code),
                "source": "pattern",
                "in_watchlist": False,
                "pattern": hit[2],
                "pattern_name": PATTERN_NAMES.get(hit[2]),
                "buy_price": buy,
                "stop_price": stop,
            }
        )
        added += 1
    return result
