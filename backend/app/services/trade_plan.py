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
    TradeJournal,
    TradePlan,
)
from app.services.patterns import PATTERNS
from app.services.stock_meta import industries_of, sectors_of
from app.services.trading_days import next_trade_day
from app.services.watch_scope import stock_watch_codes
from app.sources.base import chunked
from app.timeutil import today_cn

logger = logging.getLogger(__name__)

PATTERN_NAMES = {pattern.key: pattern.name for pattern in PATTERNS}
_KEYS = tuple(PATTERN_NAMES)

#: 计划状态（落库值 → 中文）。与「核对状态」（触发/未触发/破位…）**不是一回事**：
#: 前者是这笔计划走到哪一步，后者是日线算出来的结果。
PLAN_STATUS_LABELS = {
    "observe": "观察中",
    "pending": "待执行",
    "executed": "已执行",
    "cancelled": "取消",
    "expired": "过期",
}
PLAN_STATUSES = tuple(PLAN_STATUS_LABELS)


def _auto_status(plan: TradePlan, today: date) -> str:
    """按日期与回填情况推计划状态。

    - 填了实际买入价 → 已执行（手动回填比任何推断都准）；
    - 仅观察 → 观察中（不进执行率）；
    - 计划日在未来 → 观察中；就是今天 → 待执行；已经过去 → 过期
      （**触发过但没回填**也算过期：那说明没按计划做，核对状态里仍能看到触发）。
    """
    if plan.actual_price is not None:
        return "executed"
    if plan.observe_only:
        return "observe"
    if plan.plan_date > today:
        return "observe"
    if plan.plan_date == today:
        return "pending"
    return "expired"


def _sync_status(session: Session, plans: list[TradePlan]) -> None:
    """把自动状态同步回库；**手动改过的不覆盖**（`status_source == "manual"`）。"""
    today = today_cn()
    changed = False
    for plan in plans:
        if plan.status_source == "manual":
            continue
        auto = _auto_status(plan, today)
        if plan.status != auto or plan.status_source != "auto":
            plan.status = auto
            plan.status_source = "auto"
            changed = True
    if changed:
        session.commit()


def _calendar(session: Session) -> tuple[list[date], dict[date, int]]:
    days = list(
        session.scalars(select(TradeCalendar.trade_date).order_by(TradeCalendar.trade_date))
    )
    return days, {day: index for index, day in enumerate(days)}


def _bars(session: Session, codes: set[str], start: date) -> dict[str, dict[date, tuple]]:
    """`{code: {交易日: (开盘, 最高, 最低, 收盘)}}`，只取四价齐全的行。"""
    if not codes:
        return {}
    result: dict[str, dict[date, tuple]] = defaultdict(dict)
    rows = session.execute(
        select(
            StockDaily.code,
            StockDaily.trade_date,
            StockDaily.open,
            StockDaily.high,
            StockDaily.low,
            StockDaily.close,
        ).where(
            StockDaily.code.in_(codes),
            StockDaily.trade_date >= start,
            StockDaily.open.is_not(None),
            StockDaily.high.is_not(None),
            StockDaily.low.is_not(None),
            StockDaily.close.is_not(None),
        )
    ).all()
    for code, day, open_, high, low, close in rows:
        result[code][day] = (
            float(open_),
            float(high),
            float(low),
            float(close),
        )
    return result


def _next_returns(
    entry: float,
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
                value = round((bar[3] / entry - 1) * 100, 2)
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
        "fill_price": None,
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

    open_, high, low, close = bar
    triggered = high >= plan.buy_price
    broken = low <= plan.stop_price
    # 一字板：开 = 高 = 低，盘中只有一个价，挂单买不进 —— 不能因为「最高价碰到买点」
    # 就算触发（2026-10-18 修）。跌停一字同理不算触发。
    one_word = open_ == high == low
    if one_word:
        if high >= plan.buy_price:
            status = "一字板买不进"
        elif broken:
            status = "放弃（破位）"
        else:
            status = "未触发"
    elif triggered and broken:
        status = "同日触发并跌破"
    elif triggered:
        status = "触发"
    elif broken:
        status = "放弃（破位）"
    else:
        status = "未触发"

    # 跳空高开时按**开盘价**成交（不是计划买点价），否则收益会被高估。
    fill_price = (
        max(float(plan.buy_price), open_) if triggered and not one_word else None
    )
    result = {
        "status": status,
        "high": high,
        "low": low,
        "close": close,
        "fill_price": fill_price,
        "day_close_pct": None,
        "max_favorable_pct": None,
        "max_adverse_pct": None,
        "next_1d_pct": None,
        "next_3d_pct": None,
        "next_5d_pct": None,
    }
    if status == "触发" and fill_price is not None:
        result["day_close_pct"] = round((close / fill_price - 1) * 100, 2)
        result["max_favorable_pct"] = round((high / fill_price - 1) * 100, 2)
        result["max_adverse_pct"] = round((low / fill_price - 1) * 100, 2)
        result.update(_next_returns(fill_price, bars, calendar, pos, plan.plan_date))
    return result


def load(session: Session, user_id: int, plan_date: date | None = None) -> list[dict]:
    """该用户的计划 + 现算验证，按计划日倒序。

    读取时顺手把「计划状态」同步回库（手动改过的除外，见 `_sync_status`）——
    任务书要求自动核对结果与落库状态一致，不要只在页面上现算。
    """
    statement = select(TradePlan).where(TradePlan.user_id == user_id)
    if plan_date is not None:
        statement = statement.where(TradePlan.plan_date == plan_date)
    plans = list(
        session.scalars(statement.order_by(TradePlan.plan_date.desc(), TradePlan.id.desc()))
    )
    if not plans:
        return []

    _sync_status(session, plans)

    calendar, pos = _calendar(session)
    data_end = session.scalar(select(func.max(StockDaily.trade_date)))
    bars = _bars(session, {plan.code for plan in plans}, min(plan.plan_date for plan in plans))
    journals = _journal_map(session, user_id, [plan.id for plan in plans])
    return [
        {
            "id": plan.id,
            "code": plan.code,
            "name": plan.name,
            "plan_date": plan.plan_date,
            "buy_price": plan.buy_price,
            "stop_price": plan.stop_price,
            "note": plan.note,
            "source": plan.source,
            "source_name": plan.source_name,
            "signal_date": plan.signal_date,
            "ref_price": plan.ref_price,
            "buy_high": plan.buy_high,
            "target_price": plan.target_price,
            "position_note": plan.position_note,
            "observe_only": bool(plan.observe_only),
            "plan_status": plan.status,
            "plan_status_label": PLAN_STATUS_LABELS.get(plan.status or ""),
            "status_source": plan.status_source,
            "actual_price": plan.actual_price,
            "followed_plan": plan.followed_plan,
            "deviation": plan.deviation,
            **journals.get(plan.id, {"journal_id": None, "journal_exit_price": None}),
            **evaluate(plan, bars.get(plan.code, {}), calendar, pos, data_end),
            "created_at": plan.created_at,
            "updated_at": plan.updated_at,
        }
        for plan in plans
    ]


def _journal_map(
    session: Session, user_id: int, plan_ids: list[int]
) -> dict[int, dict]:
    """计划 → 关联日志（只取最近一条；同一条计划重复记一笔时更新原记录）。"""
    if not plan_ids:
        return {}
    rows = session.execute(
        select(
            TradeJournal.plan_id,
            TradeJournal.id,
            TradeJournal.exit_price,
        )
        .where(
            TradeJournal.user_id == user_id,
            TradeJournal.plan_id.in_(plan_ids),
        )
        .order_by(TradeJournal.id.desc())
    ).all()
    result: dict[int, dict] = {}
    for plan_id, journal_id, exit_price in rows:
        result.setdefault(
            plan_id, {"journal_id": journal_id, "journal_exit_price": exit_price}
        )
    return result


def create_from_signal(
    session: Session,
    user_id: int,
    code: str,
    pattern: str,
    signal_date: date | None = None,
    plan_date: date | None = None,
    overwrite: bool = False,
) -> tuple[dict, bool]:
    """从形态 / 悟道名单生成一条明日计划（FEAT-PLAN-LOOP A）。

    默认**不覆盖**同 (用户, 代码, 执行日) 的既有计划：用户可能已经改过价位，
    再点一次按钮只返回那一条（`created=False`），不会把编辑冲掉。

    价位来源与「候选建议」一致（`_levels`）：`breakout/watch_high` 当触发价、
    `support/wash_low/start_low` 当放弃位；缺放弃位时按触发价 × 0.95 兜底并注明
    在 `note` 里，避免造出一条永远不触发的怪计划。
    """
    digits = "".join(char for char in str(code or "").strip() if char.isdigit())[:6]
    if len(digits) != 6:
        raise ValueError("股票代码需要 6 位数字")
    basic = session.get(StockBasic, digits)
    if basic is None:
        raise ValueError(f"查不到这个代码「{digits}」")

    statement = select(PatternHit).where(
        PatternHit.code == digits, PatternHit.pattern == pattern
    )
    if signal_date is not None:
        statement = statement.where(PatternHit.trade_date == signal_date)
    hit = session.scalar(statement.order_by(PatternHit.trade_date.desc()).limit(1))
    if hit is None:
        raise LookupError("这条形态命中不存在（可能还没扫描到那一天）")

    target = plan_date or next_trade_day(hit.trade_date, session)
    if target is None:
        raise ValueError("交易日历里没有下一个交易日，先补日历再生成计划")

    existing = session.scalar(
        select(TradePlan).where(
            TradePlan.user_id == user_id,
            TradePlan.code == digits,
            TradePlan.plan_date == target,
        )
    )
    if existing is not None and not overwrite:
        rows = load(session, user_id, target)
        for row in rows:
            if row["id"] == existing.id:
                return row, False
        raise LookupError("这条计划已存在，但读取失败")

    buy, stop = _levels(hit.key_levels)
    buy = buy or hit.close
    if not buy:
        raise ValueError("这条命中没有可用价位，请手工建计划")
    fallback_stop = round(float(buy) * 0.95, 2)
    stop = stop if stop and stop < buy else fallback_stop

    plan = existing or TradePlan(user_id=user_id, code=digits, plan_date=target)
    if existing is None:
        session.add(plan)
    plan.name = hit.name or basic.name
    plan.buy_price = round(float(buy), 2)
    plan.stop_price = round(float(stop), 2)
    plan.source = pattern
    plan.source_name = PATTERN_NAMES.get(pattern, pattern)
    plan.signal_date = hit.trade_date
    plan.ref_price = hit.close
    if existing is None and stop == fallback_stop:
        plan.note = "放弃位由系统按触发价 × 0.95 兜底，请按自己的规则改"
    plan.status_source = None  # 新计划交给自动状态
    session.commit()
    session.refresh(plan)

    rows = load(session, user_id, target)
    for row in rows:
        if row["id"] == plan.id:
            return row, True
    raise LookupError("计划已保存，但读取失败")


def stats(session: Session, user_id: int, start: date, end: date) -> dict:
    """周度 / 区间执行质量（FEAT-PLAN-LOOP D）。

    只统计**区间内计划日**的计划；执行率 = 已执行 ÷（总数 − 仅观察）。
    「按计划执行率」= `followed_plan=True` ÷ 已执行。盈亏只用**关联日志且已平仓**
    的记录算（`exit_price` 非空），没有就返回 null，不拿 0 顶。
    """
    plans = list(
        session.scalars(
            select(TradePlan).where(
                TradePlan.user_id == user_id,
                TradePlan.plan_date >= start,
                TradePlan.plan_date <= end,
            )
        )
    )
    _sync_status(session, plans)

    counts = {key: 0 for key in PLAN_STATUSES}
    for plan in plans:
        counts[plan.status or "observe"] = counts.get(plan.status or "observe", 0) + 1
    observable = len(plans) - counts["observe"]
    executed = counts["executed"]
    followed = sum(1 for plan in plans if plan.actual_price is not None and plan.followed_plan)

    journals = list(
        session.scalars(
            select(TradeJournal).where(
                TradeJournal.user_id == user_id,
                TradeJournal.plan_id.in_([plan.id for plan in plans] or [-1]),
            )
        )
    )
    closed = [
        journal
        for journal in journals
        if journal.entry_price and journal.exit_price
    ]
    returns = [
        (journal.exit_price / journal.entry_price - 1) * 100 for journal in closed
    ]
    wins = sum(1 for value in returns if value > 0)

    by_deviation: dict[str, int] = defaultdict(int)
    for plan in plans:
        if plan.deviation:
            by_deviation[plan.deviation] += 1

    def rate(part: int, whole: int) -> float | None:
        return round(part / whole * 100, 1) if whole else None

    note = None
    if not closed:
        note = "区间内没有已平仓的关联日志，只能看执行率；平仓后再回来看盈亏。"

    return {
        "start": start,
        "end": end,
        "plans": len(plans),
        "observable": observable,
        "executed": executed,
        "cancelled": counts["cancelled"],
        "expired": counts["expired"],
        "pending": counts["pending"],
        "observe": counts["observe"],
        "execution_rate": rate(executed, observable),
        "followed": followed,
        "follow_rate": rate(followed, executed),
        "linked_journals": len(journals),
        "closed": len(closed),
        "win_rate": rate(wins, len(closed)),
        "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
        "by_deviation": dict(by_deviation),
        "note": note,
    }


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

    # 只算股票：自选里可能混着可转债代码（可转债-5），给它们排计划没有意义
    watch = set(stock_watch_codes(session, user_id))
    # 单日命中的票可能上千只，按 900 分批（与 api/patterns、stock_phase 同一约定），
    # 不依赖 SQLite 版本对单条 SQL 变量数的上限
    names: dict[str, str | None] = {}
    for batch in chunked(sorted(watch | set(best)), 900):
        names.update(
            session.execute(
                select(StockBasic.code, StockBasic.name).where(
                    StockBasic.code.in_(batch)
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

    # 板块 / 行业走 `stock_meta`，与形态页同一口径：这一屏要回答「这只票是什么、
    # 做过什么题材」，只给形态名不够。按最终名单查一次，不按候选池查（后者会白查
    # 被 `added >= 10` 截掉的票）。
    codes = {row["code"] for row in result}
    sectors = sectors_of(session, codes)
    industries = industries_of(session, codes)
    for row in result:
        row["sectors"] = sectors.get(row["code"], [])
        row["industry"] = industries.get(row["code"])
    return result
