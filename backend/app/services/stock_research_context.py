"""个股「观察日研究依据」只读解释链（S3-C）。

目标：回答三个严格分开的问题：

1. **观察日当天收盘时已知什么**（`as_known_on_day`）；
2. **哪些资料是盘后才拿到 / 来源时间未知**（evidence 的 `known_by`）；
3. **D+1 后验结果**（`later_outcome`，只供复盘，绝不反写 D 日依据）。

边界：
- 只读本地库（limit_pool / stock_daily / market_sentiment / sector_member /
  stock_concept / collect_log / trade_calendar / stock_basic）；
- 不调用 `syncStock`、不抓公告/题材/龙虎榜/外部行情，不写库；
- 历史时点不能证明「当时已知」时一律 `UNKNOWN`，不猜 true；
- 观察日不存在 / 非交易日 → `ValueError`，由 API 转 400，不悄悄滚到别的日期。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CollectLog,
    LimitPool,
    MarketSentiment,
    SectorBasic,
    SectorMember,
    StockBasic,
    StockConcept,
    StockDaily,
    TradeCalendar,
)
from app.timeutil import today_cn

CODE_PATTERN = re.compile(r"^\d{6}$")

STATUS_VERIFIED = "VERIFIED"
STATUS_PARTIAL = "PARTIAL"
STATUS_PENDING = "PENDING"
STATUS_UNVERIFIABLE = "UNVERIFIABLE"
STATUS_NOT_COLLECTED = "NOT_COLLECTED"

KNOWN_CLOSE = "CLOSE"
KNOWN_PRIOR_DAY = "PRIOR_DAY"
KNOWN_SAME_DAY_UNKNOWN = "SAME_DAY_UNKNOWN"
KNOWN_LATER = "LATER"
KNOWN_POST_CLOSE = "POST_CLOSE_UNKNOWN"
KNOWN_FORWARD = "FORWARD"

NOTES = [
    "「观察日已知」按收盘时点解释；09:25 竞价时点只会更严格，不会更宽松。",
    "题材快照的 fetched_at 是本系统写入时间，不能证明上游盘前已发布。",
    "D+1 结果只供复盘，不反写 D 日依据，也不进入 C0 或任何正式信号。",
]


def _pool_quality(
    log: CollectLog | None, rows: int, *, allow_empty: bool = False
) -> tuple[str, bool, str | None]:
    """返回 (quality, trusted, reason_code)。与 P0-B 的池质量同一口径。"""
    if log is None:
        return (
            (STATUS_NOT_COLLECTED if rows == 0 else STATUS_UNVERIFIABLE),
            False,
            "POOL_LOG_MISSING" if rows == 0 else "POOL_LOG_MISSING_WITH_ROWS",
        )
    status = (log.status or "").lower()
    if status == "ok":
        if rows == 0 and not allow_empty:
            return STATUS_UNVERIFIABLE, False, "UP_POOL_EMPTY_WITH_OK_LOG"
        return STATUS_VERIFIED, True, None
    if status in ("partial", "failed", "skipped", "empty"):
        return STATUS_UNVERIFIABLE, False, f"POOL_STATUS_{status.upper()}"
    return STATUS_UNVERIFIABLE, False, "POOL_STATUS_UNKNOWN"


def _theme_known_by(fetched_at, observation: date) -> str:
    if fetched_at is None:
        return "UNKNOWN"
    day = fetched_at.date() if hasattr(fetched_at, "date") else None
    if day is None:
        return "UNKNOWN"
    if day < observation:
        return KNOWN_PRIOR_DAY
    if day > observation:
        return KNOWN_LATER
    return KNOWN_SAME_DAY_UNKNOWN


def _latest_logs(
    session: Session, days: list[date], tasks: tuple[str, ...]
) -> dict[tuple[date, str], CollectLog]:
    if not days:
        return {}
    rows = session.scalars(
        select(CollectLog)
        .where(CollectLog.trade_date.in_(days), CollectLog.task.in_(tasks))
        .order_by(CollectLog.id.desc())
    ).all()
    latest: dict[tuple[date, str], CollectLog] = {}
    for row in rows:
        if row.trade_date is not None:
            latest.setdefault((row.trade_date, row.task), row)
    return latest


def build(
    session: Session, *, code: str, as_of: date | None = None
) -> dict[str, Any]:
    """构建一只股票在观察日的已知 / 未知 / 后验三段式依据。"""
    if not CODE_PATTERN.match(code or ""):
        raise ValueError("股票代码必须是 6 位数字")
    basic = session.get(StockBasic, code)
    if basic is None:
        raise ValueError(f"查不到这个代码「{code}」，请确认后重试")

    calendar = list(
        session.scalars(select(TradeCalendar.trade_date).order_by(TradeCalendar.trade_date))
    )
    if not calendar:
        raise ValueError("交易日历为空，无法确定观察日")
    position = {day: index for index, day in enumerate(calendar)}

    if as_of is None:
        today = today_cn()
        candidates = [day for day in calendar if day <= today]
        if not candidates:
            raise ValueError("没有可用的观察日")
        resolved = candidates[-1]
    else:
        if as_of not in position:
            raise ValueError(f"{as_of} 不是交易日，不能作为观察日")
        resolved = as_of

    index = position[resolved]
    previous = calendar[index - 1] if index > 0 else None
    following = calendar[index + 1] if index + 1 < len(calendar) else None

    pool_rows = session.scalars(
        select(LimitPool).where(
            LimitPool.trade_date == resolved, LimitPool.code == code
        )
    ).all()
    membership = {row.pool_type: row for row in pool_rows}

    logs = _latest_logs(
        session, [day for day in (resolved, following) if day], ("pool_up", "pool_broken", "pool_down")
    )
    up_log = logs.get((resolved, "pool_up"))
    broken_log = logs.get((resolved, "pool_broken"))
    down_log = logs.get((resolved, "pool_down"))
    # 池行数按当天全市场统计（不是这只票），用于判断 ok+0 异常
    up_rows = session.query(LimitPool).filter(
        LimitPool.trade_date == resolved, LimitPool.pool_type == "up"
    ).count()
    broken_rows = session.query(LimitPool).filter(
        LimitPool.trade_date == resolved, LimitPool.pool_type == "broken"
    ).count()
    down_rows = session.query(LimitPool).filter(
        LimitPool.trade_date == resolved, LimitPool.pool_type == "down"
    ).count()
    up_quality, up_trusted, up_reason = _pool_quality(up_log, up_rows)
    broken_quality, broken_trusted, broken_reason = _pool_quality(
        broken_log, broken_rows, allow_empty=True
    )
    down_quality, down_trusted, down_reason = _pool_quality(
        down_log, down_rows, allow_empty=True
    )

    daily = session.get(StockDaily, (resolved, code))
    sentiment = session.get(MarketSentiment, resolved)
    theme_rows = session.execute(
        select(
            SectorMember.sector_code,
            SectorMember.name,
            SectorMember.fetched_at,
            SectorBasic.name,
            SectorBasic.taxonomy,
        )
        .join(SectorBasic, SectorBasic.code == SectorMember.sector_code)
        .where(SectorMember.trade_date == resolved, SectorMember.code == code)
        .order_by(SectorBasic.taxonomy, SectorBasic.name)
    ).all()
    concept_rows = session.scalars(
        select(StockConcept)
        .where(StockConcept.trade_date == resolved, StockConcept.code == code)
        .order_by(StockConcept.concept)
    ).all()

    # ---------------- 观察日收盘时已知
    limit_status = "UNKNOWN"
    board_height = None
    if "up" in membership and up_trusted:
        limit_status = "UP"
        board_height = membership["up"].consecutive
    elif "broken" in membership and broken_trusted:
        limit_status = "BROKEN"
    elif "down" in membership and down_trusted:
        limit_status = "DOWN"
    elif up_trusted and broken_trusted and down_trusted:
        limit_status = "NOT_IN_POOL"

    themes = [
        {
            "name": sector_name or sector_code,
            "sector_code": sector_code,
            "taxonomy": taxonomy,
            "fetched_at": fetched_at,
            "known_by": _theme_known_by(fetched_at, resolved),
        }
        for sector_code, _member_name, fetched_at, sector_name, taxonomy in theme_rows
    ]
    concepts = [
        {
            "name": row.concept,
            "sector_code": None,
            "taxonomy": "stock_concept",
            "fetched_at": None,
            # 涨停天梯通常是盘后生成，没有可证明的发布时刻
            "known_by": KNOWN_POST_CLOSE,
        }
        for row in concept_rows
    ]
    as_known = {
        "previous_close_date": previous,
        "limit_status": limit_status,
        "board_height": board_height,
        "observed_pool_membership": sorted(membership),
        "pool_quality": {
            "up": up_quality,
            "broken": broken_quality,
            "down": down_quality,
        },
        "daily": (
            {
                "trade_date": resolved,
                "close": daily.close,
                "pct_chg": daily.pct_chg,
                "known_by": KNOWN_CLOSE,
            }
            if daily is not None
            else None
        ),
        "themes": themes,
        "concepts": concepts,
        "sentiment": (
            {
                "limit_up_count": sentiment.limit_up_count,
                "broken_rate": sentiment.broken_rate,
                "max_consecutive": sentiment.max_consecutive,
                "up_count": sentiment.up_count,
                "down_count": sentiment.down_count,
            }
            if sentiment is not None
            else None
        ),
    }

    # ---------------- D+1 后验（只读，不反写）
    data_as_of = session.scalar(select(func_max_stock_daily()))
    if following is None:
        outcome_status = STATUS_PENDING
        outcome_reason = "NO_NEXT_TRADE_DAY"
    else:
        outcome_daily = session.get(StockDaily, (following, code))
        next_up_log = logs.get((following, "pool_up"))
        next_up_rows = session.query(LimitPool).filter(
            LimitPool.trade_date == following, LimitPool.pool_type == "up"
        ).count()
        next_up_quality, next_up_trusted, _ = _pool_quality(next_up_log, next_up_rows)
        if outcome_daily is None:
            if data_as_of is None or data_as_of < following:
                outcome_status, outcome_reason = STATUS_PENDING, "OUTCOME_PENDING"
            else:
                outcome_status, outcome_reason = STATUS_UNVERIFIABLE, "DAILY_MISSING"
            outcome_pct = None
            outcome_close = None
            again = None
        else:
            outcome_pct = outcome_daily.pct_chg
            outcome_close = outcome_daily.close
            outcome_status = STATUS_VERIFIED if next_up_trusted else STATUS_PARTIAL
            outcome_reason = None if next_up_trusted else "OUTCOME_LIMIT_POOL_UNVERIFIED"
            again = None
            if next_up_trusted:
                again = session.get(LimitPool, (following, code, "up")) is not None
    later_outcome = {
        "outcome_date": following,
        "status": outcome_status,
        "pct_chg": outcome_pct if following is not None else None,
        "close": outcome_close if following is not None else None,
        "limit_up_again": again if following is not None else None,
        "reason_code": outcome_reason,
        "known_by": KNOWN_FORWARD,
    }

    # ---------------- 质量
    reasons: list[str] = []
    if up_quality != STATUS_VERIFIED:
        reasons.append(up_reason or "POOL_UP_UNVERIFIED")
    if not themes:
        reasons.append("NO_THEME_SNAPSHOT")
    if any(item["known_by"] != KNOWN_PRIOR_DAY for item in themes):
        reasons.append("THEME_PIT_UNKNOWN")
    if concepts:
        reasons.append("CONCEPT_POST_CLOSE_UNKNOWN")
    if outcome_status in (STATUS_PENDING, STATUS_PARTIAL, STATUS_UNVERIFIABLE):
        reasons.append(outcome_reason or "OUTCOME_NOT_VERIFIED")

    if up_quality == STATUS_NOT_COLLECTED and daily is None:
        status = STATUS_NOT_COLLECTED
    elif up_quality != STATUS_VERIFIED:
        status = STATUS_UNVERIFIABLE
    else:
        status = STATUS_PARTIAL

    # ---------------- 证据链
    evidence: list[dict[str, Any]] = []

    def add(
        claim: str,
        table: str,
        source_day: date | None,
        fetched_at,
        known_by: str,
        quality: str,
        reason: str | None,
        forward: bool,
        summary: str,
    ) -> None:
        evidence.append(
            {
                "claim": claim,
                "source_table": table,
                "source_trade_date": source_day,
                "fetched_at": fetched_at,
                "known_by": known_by,
                "quality": quality,
                "reason_code": reason,
                "is_forward_known": forward,
                "summary": summary,
            }
        )

    if membership:
        labels = "、".join(sorted(membership))
        add(
            "观察日池记录",
            "limit_pool",
            resolved,
            up_log.created_at if up_log else None,
            KNOWN_CLOSE,
            up_quality,
            up_reason,
            False,
            f"当日观察到 {labels}；涨停池质量 {up_quality}",
        )
    else:
        add(
            "观察日池记录",
            "limit_pool",
            resolved,
            up_log.created_at if up_log else None,
            KNOWN_CLOSE,
            up_quality,
            up_reason,
            False,
            "当日没有该股的涨停/炸板/跌停记录",
        )
    if daily is not None:
        add(
            "观察日日线",
            "stock_daily",
            resolved,
            None,
            KNOWN_CLOSE,
            STATUS_VERIFIED,
            None,
            False,
            f"收盘 {daily.close}，涨跌幅 {daily.pct_chg}",
        )
    else:
        add(
            "观察日日线",
            "stock_daily",
            resolved,
            None,
            KNOWN_CLOSE,
            STATUS_NOT_COLLECTED,
            "DAILY_MISSING",
            False,
            "观察日没有本地日线",
        )
    if sentiment is not None:
        add(
            "观察日市场情绪",
            "market_sentiment",
            resolved,
            None,
            KNOWN_CLOSE,
            STATUS_VERIFIED,
            None,
            False,
            f"涨停 {sentiment.limit_up_count} 家，最高 {sentiment.max_consecutive} 板",
        )
    if themes:
        add(
            "题材归属快照",
            "sector_member",
            resolved,
            max((item["fetched_at"] for item in themes if item["fetched_at"]), default=None),
            themes[0]["known_by"],
            STATUS_PARTIAL,
            "THEME_PIT_UNKNOWN",
            False,
            "；".join(f"{item['name']}({item['taxonomy']})" for item in themes),
        )
    if concepts:
        add(
            "涨停天梯题材",
            "stock_concept",
            resolved,
            None,
            KNOWN_POST_CLOSE,
            STATUS_PARTIAL,
            "CONCEPT_POST_CLOSE_UNKNOWN",
            False,
            "、".join(item["name"] for item in concepts),
        )
    if following is not None:
        add(
            "D+1 后验结果",
            "stock_daily",
            following,
            None,
            KNOWN_FORWARD,
            outcome_status,
            outcome_reason,
            True,
            (
                f"D+1 涨跌幅 {outcome_pct}，收盘 {outcome_close}"
                if outcome_pct is not None
                else "D+1 行情未到齐或不可验证"
            ),
        )

    return {
        "code": code,
        "name": basic.name,
        "requested_as_of": as_of,
        "resolved_trade_date": resolved,
        "data_as_of": data_as_of,
        "historical_signal_eligible": False,
        "quality": {"status": status, "reasons": _dedupe(reasons)},
        "as_known_on_day": as_known,
        "later_outcome": later_outcome,
        "evidence": evidence,
        "notes": NOTES,
    }


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    for item in items:
        if item and item not in result:
            result.append(item)
    return result


def func_max_stock_daily():
    """`select(max(stock_daily.trade_date))` 的表达式（便于单测替换）。"""
    from sqlalchemy import func

    return func.max(StockDaily.trade_date)
