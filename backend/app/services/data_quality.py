"""盘后数据可信度控制台（S4-B）：只读诊断，不补采、不建表、不修库。

回答的是「为什么今天某个榜单是空的 / 为什么不能拿它做历史信号」，而不是
「帮你把数据补回来」。硬边界：

- 只 `SELECT`；不调用任何外部源，不写缓存，不触发采集；
- 每个来源给出**有来源限定**的状态、原因码、已观察行数与最新观测日；
- 没有完成证明的来源（`sector_member`）最高只能是 `PARTIAL`，不升级成 VERIFIED；
- 最新一次采集失败**不抹掉**已经持久化的旧快照：分别输出 `latest_attempt`
  与 `latest_valid_snapshot`，并在原因码里说明「行情保留、最近尝试失败」；
- 不把 `StockBasic` 当前数量当成历史应有股票数（退市/停牌/上市变化没有口径）；
- `days` 有上限，所有查询按日期批量 `GROUP BY`，不做逐证券 N+1。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models import (
    AuctionSnapshot,
    CollectLog,
    LimitPool,
    MarketSentiment,
    SectorBasic,
    SectorMember,
    StockDaily,
    TradeCalendar,
)
from app.timeutil import today_cn

STATUS_VERIFIED = "VERIFIED_SOURCE"
STATUS_PARTIAL = "PARTIAL"
STATUS_UNVERIFIABLE = "UNVERIFIABLE"
STATUS_NOT_COLLECTED = "NOT_COLLECTED"
STATUS_PENDING = "PENDING"
STATUS_NOT_TRADE_DAY = "NOT_TRADE_DAY"

SOURCES = (
    "stock_daily",
    "limit_pool",
    "sector_member",
    "market_sentiment",
    "auction_snapshot",
    "trade_calendar",
)

MAX_DAYS = 250
MIN_DAYS = 1

POOL_TASKS = ("pool_up", "pool_broken", "pool_down")
FRAME_TASK = "auction_frame"
RESULT_FRAME = "0926"

NOTE_NO_COMPLETION = "仅说明已观察到的板块，不代表成员全量"
NOTE_SECTOR_CATALOG = "expected_sectors 取当前板块目录，不是历史应有板块数"
NOTE_DAILY = "行数来自本地 stock_daily；不把当前股票数量当历史应有数"
NOTE_AUCTION = "竞价来源为腾讯批量行情；真实交易日验收仍未完成"


def _validate_days(days: int) -> int:
    if days < MIN_DAYS or days > MAX_DAYS:
        raise ValueError(f"days 必须在 {MIN_DAYS}~{MAX_DAYS} 之间")
    return days


def _database_name(session: Session) -> str | None:
    try:
        database = session.get_bind().url.database
    except Exception:  # noqa: BLE001 - 只用于展示，拿不到就不显示
        return None
    if not database:
        return None
    return database.replace("\\", "/").rsplit("/", 1)[-1]


def _trade_days(session: Session, end: date, days: int) -> list[date]:
    return list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date <= end)
            .order_by(TradeCalendar.trade_date.desc())
            .limit(days)
        )
    )[::-1]


def _item(
    source: str,
    trade_date: date | None,
    status: str,
    *,
    reasons: list[str] | None = None,
    observed_rows: int = 0,
    expected_rows: int | None = None,
    observed_sectors: int | None = None,
    expected_sectors: int | None = None,
    completion_proof: bool = False,
    latest_observation_date: date | None = None,
    latest_valid_snapshot: date | None = None,
    latest_attempt: dict[str, Any] | None = None,
    note: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": source,
        "trade_date": trade_date,
        "status": status,
        "reason_codes": reasons or [],
        "observed_rows": observed_rows,
        "expected_rows": expected_rows,
        "observed_sectors": observed_sectors,
        "expected_sectors": expected_sectors,
        "completion_proof": completion_proof,
        "latest_observation_date": latest_observation_date,
        "latest_valid_snapshot": latest_valid_snapshot,
        "latest_attempt": latest_attempt,
        "note": note,
    }
    if extra:
        payload.update(extra)
    return payload


def _log_brief(log: CollectLog | None) -> dict[str, Any] | None:
    if log is None:
        return None
    return {
        "status": log.status,
        "rows": log.rows,
        "created_at": log.created_at,
        "message": (log.message or "")[:200],
    }


# ------------------------------------------------------------------ 各来源


def _stock_daily_items(session: Session, days: list[date]) -> dict[date, dict[str, Any]]:
    latest = session.scalar(select(func.max(StockDaily.trade_date)))
    rows = session.execute(
        select(
            StockDaily.trade_date,
            func.count(),
            func.count(StockDaily.close),
            func.count(StockDaily.pct_chg),
        )
        .where(StockDaily.trade_date.in_(days))
        .group_by(StockDaily.trade_date)
    ).all()
    counts = {row[0]: (row[1], row[2], row[3]) for row in rows}
    items: dict[date, dict[str, Any]] = {}
    for day in days:
        observed, with_close, with_pct = counts.get(day, (0, 0, 0))
        if observed == 0:
            items[day] = _item(
                "stock_daily",
                day,
                STATUS_NOT_COLLECTED,
                reasons=["DAILY_NOT_COLLECTED"],
                latest_observation_date=latest,
                note=NOTE_DAILY,
            )
            continue
        missing = observed - min(with_close, with_pct)
        reasons = ["DAILY_FIELDS_NULL"] if missing > 0 else []
        items[day] = _item(
            "stock_daily",
            day,
            STATUS_PARTIAL if missing > 0 else STATUS_VERIFIED,
            reasons=reasons,
            observed_rows=observed,
            latest_observation_date=latest,
            latest_valid_snapshot=day,
            note=NOTE_DAILY,
            extra={"rows_with_close": with_close, "rows_with_pct_chg": with_pct},
        )
    return items


def _limit_pool_items(session: Session, days: list[date]) -> dict[date, dict[str, Any]]:
    latest = session.scalar(select(func.max(LimitPool.trade_date)))
    rows = session.execute(
        select(LimitPool.trade_date, LimitPool.pool_type, func.count())
        .where(LimitPool.trade_date.in_(days))
        .group_by(LimitPool.trade_date, LimitPool.pool_type)
    ).all()
    counts: dict[date, dict[str, int]] = {}
    for day, pool_type, total in rows:
        counts.setdefault(day, {})[pool_type] = total

    logs = session.scalars(
        select(CollectLog)
        .where(CollectLog.trade_date.in_(days), CollectLog.task.in_(POOL_TASKS))
        .order_by(CollectLog.id.desc())
    ).all()
    latest_any: dict[tuple[date, str], CollectLog] = {}
    latest_ok: dict[tuple[date, str], CollectLog] = {}
    for log in logs:
        if log.trade_date is None:
            continue
        key = (log.trade_date, log.task)
        latest_any.setdefault(key, log)
        if (log.status or "").lower() == "ok":
            latest_ok.setdefault(key, log)

    valid_days = [day for day in days if counts.get(day, {}).get("up", 0) > 0 and (day, "pool_up") in latest_ok]
    items: dict[date, dict[str, Any]] = {}
    for day in days:
        day_counts = counts.get(day, {})
        up_rows = day_counts.get("up", 0)
        broken_rows = day_counts.get("broken", 0)
        down_rows = day_counts.get("down", 0)
        observed = up_rows + broken_rows + down_rows
        up_log = latest_any.get((day, "pool_up"))
        up_ok = latest_ok.get((day, "pool_up"))
        reasons: list[str] = []
        status = STATUS_VERIFIED
        note = None

        if observed == 0 and up_log is None:
            status = STATUS_NOT_COLLECTED
            reasons.append("POOL_NOT_COLLECTED")
        elif up_rows == 0:
            status = STATUS_UNVERIFIABLE
            if up_ok is not None:
                reasons.append("UP_POOL_EMPTY_WITH_OK_LOG")
            elif up_log is None:
                reasons.append("POOL_LOG_MISSING_WITH_ROWS")
            else:
                reasons.append(f"POOL_STATUS_{(up_log.status or 'UNKNOWN').upper()}")
        elif up_log is None:
            status = STATUS_UNVERIFIABLE
            reasons.append("POOL_LOG_MISSING_WITH_ROWS")
        elif (up_log.status or "").lower() != "ok":
            if up_ok is not None:
                # 最新一次尝试失败，但旧快照仍在 → 不能抹掉已持久化的有效行
                status = STATUS_PARTIAL
                reasons.append("LATEST_ATTEMPT_FAILED_SNAPSHOT_RETAINED")
            else:
                status = STATUS_UNVERIFIABLE
                reasons.append(f"POOL_STATUS_{(up_log.status or 'UNKNOWN').upper()}")
        for task, label in (("pool_broken", "BROKEN"), ("pool_down", "DOWN")):
            log = latest_any.get((day, task))
            if log is None:
                reasons.append(f"{label}_POOL_LOG_MISSING")
            elif (log.status or "").lower() != "ok":
                reasons.append(f"{label}_POOL_STATUS_{(log.status or 'UNKNOWN').upper()}")

        items[day] = _item(
            "limit_pool",
            day,
            status,
            reasons=reasons,
            observed_rows=observed,
            completion_proof=status == STATUS_VERIFIED,
            latest_observation_date=latest,
            latest_valid_snapshot=(
                max(item for item in valid_days if item <= day)
                if any(item <= day for item in valid_days)
                else None
            ),
            latest_attempt=_log_brief(up_log),
            note=note,
            extra={
                "up_rows": up_rows,
                "broken_rows": broken_rows,
                "down_rows": down_rows,
            },
        )
    return items


def _sector_member_items(session: Session, days: list[date]) -> dict[date, dict[str, Any]]:
    latest = session.scalar(select(func.max(SectorMember.trade_date)))
    expected_by_taxonomy = {
        taxonomy: total
        for taxonomy, total in session.execute(
            select(SectorBasic.taxonomy, func.count()).group_by(SectorBasic.taxonomy)
        ).all()
    }
    rows = session.execute(
        select(
            SectorMember.trade_date,
            SectorBasic.taxonomy,
            func.count(),
            func.count(func.distinct(SectorMember.sector_code)),
        )
        .join(SectorBasic, SectorBasic.code == SectorMember.sector_code)
        .where(SectorMember.trade_date.in_(days))
        .group_by(SectorMember.trade_date, SectorBasic.taxonomy)
    ).all()
    by_day: dict[date, list[dict[str, Any]]] = {}
    for day, taxonomy, total, sectors in rows:
        by_day.setdefault(day, []).append(
            {
                "taxonomy": taxonomy,
                "observed_rows": total,
                "observed_sectors": sectors,
                "expected_sectors": expected_by_taxonomy.get(taxonomy, 0),
                "completion_proof": False,
            }
        )
    items: dict[date, dict[str, Any]] = {}
    for day in days:
        detail = by_day.get(day, [])
        observed_rows = sum(item["observed_rows"] for item in detail)
        observed_sectors = sum(item["observed_sectors"] for item in detail)
        expected_sectors = sum(item["expected_sectors"] for item in detail)
        if observed_rows == 0:
            items[day] = _item(
                "sector_member",
                day,
                STATUS_NOT_COLLECTED,
                reasons=["NO_MEMBERS_SNAPSHOT"],
                expected_sectors=expected_sectors or None,
                latest_observation_date=latest,
                note=NOTE_NO_COMPLETION,
            )
            continue
        items[day] = _item(
            "sector_member",
            day,
            STATUS_PARTIAL,
            reasons=["NO_MEMBERS_COMPLETION_LOG"],
            observed_rows=observed_rows,
            observed_sectors=observed_sectors,
            expected_sectors=expected_sectors or None,
            completion_proof=False,
            latest_observation_date=latest,
            latest_valid_snapshot=day,
            note=f"{NOTE_NO_COMPLETION}；{NOTE_SECTOR_CATALOG}",
            extra={"taxonomies": detail},
        )
    return items


def _market_sentiment_items(session: Session, days: list[date]) -> dict[date, dict[str, Any]]:
    latest = session.scalar(select(func.max(MarketSentiment.trade_date)))
    rows = {
        row.trade_date: row
        for row in session.scalars(
            select(MarketSentiment).where(MarketSentiment.trade_date.in_(days))
        )
    }
    items: dict[date, dict[str, Any]] = {}
    for day in days:
        row = rows.get(day)
        if row is None:
            items[day] = _item(
                "market_sentiment",
                day,
                STATUS_NOT_COLLECTED,
                reasons=["SENTIMENT_NOT_COLLECTED"],
                latest_observation_date=latest,
            )
            continue
        missing = [
            name
            for name in ("limit_up_count", "broken_rate", "max_consecutive")
            if getattr(row, name) is None
        ]
        items[day] = _item(
            "market_sentiment",
            day,
            STATUS_PARTIAL if missing else STATUS_VERIFIED,
            reasons=["SENTIMENT_FIELDS_NULL"] if missing else [],
            observed_rows=1,
            latest_observation_date=latest,
            latest_valid_snapshot=day,
            extra={"missing_fields": missing},
        )
    return items


def _auction_items(session: Session, days: list[date]) -> dict[date, dict[str, Any]]:
    latest = session.scalar(select(func.max(AuctionSnapshot.trade_date)))
    rows = dict(
        session.execute(
            select(AuctionSnapshot.trade_date, func.count())
            .where(AuctionSnapshot.trade_date.in_(days))
            .group_by(AuctionSnapshot.trade_date)
        ).all()
    )
    logs = session.scalars(
        select(CollectLog)
        .where(CollectLog.trade_date.in_(days), CollectLog.task == FRAME_TASK)
        .order_by(CollectLog.id.desc())
    ).all()
    latest_attempt: dict[date, dict[str, Any]] = {}
    effective_frames: dict[date, set[str]] = {}
    for log in logs:
        if log.trade_date is None:
            continue
        try:
            payload = json.loads(log.message or "{}")
        except (TypeError, ValueError):
            payload = {}
        frame = str(payload.get("scheduled_frame") or "")
        if log.trade_date not in latest_attempt:
            latest_attempt[log.trade_date] = {
                "status": log.status,
                "frame": frame or None,
                "created_at": log.created_at,
            }
        if frame and (log.status or "").lower() in ("ok", "partial"):
            effective_frames.setdefault(log.trade_date, set()).add(frame)

    items: dict[date, dict[str, Any]] = {}
    for day in days:
        observed = rows.get(day, 0)
        frames = effective_frames.get(day, set())
        has_result = RESULT_FRAME in frames
        has_open = "0931" in frames
        reasons: list[str] = []
        if observed == 0:
            status = STATUS_UNVERIFIABLE if (day in latest_attempt) else STATUS_NOT_COLLECTED
            reasons.append("AUCTION_NOT_COLLECTED")
        elif not has_result:
            status = STATUS_PARTIAL
            reasons.append("AUCTION_0926_MISSING")
        else:
            status = STATUS_VERIFIED
            reasons.append("REAL_AUCTION_DATA_NOT_YET_VERIFIED")
        if not has_open:
            reasons.append("AUCTION_0931_MISSING")
        attempt = latest_attempt.get(day)
        if attempt and str(attempt.get("status") or "").lower() == "failed" and observed > 0:
            reasons.append("LATEST_ATTEMPT_FAILED_SNAPSHOT_RETAINED")
        items[day] = _item(
            "auction_snapshot",
            day,
            status,
            reasons=reasons,
            observed_rows=observed,
            completion_proof=bool(observed and has_result),
            latest_observation_date=latest,
            latest_valid_snapshot=day if observed and has_result else None,
            latest_attempt=attempt,
            note=NOTE_AUCTION,
            extra={"frames": sorted(frames), "has_0926": has_result, "has_0931": has_open},
        )
    return items


def _calendar_item(session: Session, target: date) -> dict[str, Any]:
    latest = session.scalar(select(func.max(TradeCalendar.trade_date)))
    count = session.scalar(select(func.count()).select_from(TradeCalendar)) or 0
    if count == 0:
        return _item(
            "trade_calendar",
            target,
            STATUS_NOT_COLLECTED,
            reasons=["CALENDAR_EMPTY"],
            latest_observation_date=latest,
        )
    is_trade_day = session.get(TradeCalendar, target) is not None
    if is_trade_day:
        return _item(
            "trade_calendar",
            target,
            STATUS_VERIFIED,
            observed_rows=1,
            latest_observation_date=latest,
            latest_valid_snapshot=target,
            extra={"is_trade_day": True},
        )
    if target > today_cn():
        status, reason = STATUS_PENDING, "FUTURE_DATE"
    else:
        status, reason = STATUS_NOT_TRADE_DAY, "NOT_A_TRADE_DAY"
    return _item(
        "trade_calendar",
        target,
        status,
        reasons=[reason],
        latest_observation_date=latest,
        extra={"is_trade_day": False},
    )


def _safe_source(fn, session: Session, days: list[date]) -> dict[date, dict[str, Any]]:
    """单表缺失/损坏只影响这个来源，不拖垮整页；**不建表、不修库**。"""
    try:
        return fn(session, days)
    except OperationalError:
        return {
            day: _item(
                "unknown",
                day,
                STATUS_NOT_COLLECTED,
                reasons=["DB_TABLE_MISSING"],
            )
            for day in days
        }


def _source_items(session: Session, days: list[date]) -> dict[str, dict[date, dict[str, Any]]]:
    return {
        "stock_daily": _safe_source(_stock_daily_items, session, days),
        "limit_pool": _safe_source(_limit_pool_items, session, days),
        "sector_member": _safe_source(_sector_member_items, session, days),
        "market_sentiment": _safe_source(_market_sentiment_items, session, days),
        "auction_snapshot": _safe_source(_auction_items, session, days),
    }


def overview(
    session: Session, *, target_date: date | None = None, days: int = 30
) -> dict[str, Any]:
    """某个交易日的全来源快照（只读）。"""
    days = _validate_days(days)
    resolved = target_date or today_cn()
    window = _trade_days(session, resolved, days)

    if resolved > today_cn():
        items = [
            _item(
                source,
                resolved,
                STATUS_PENDING,
                reasons=["FUTURE_DATE"],
                latest_observation_date=None,
            )
            for source in SOURCES
        ]
        items[-1] = _calendar_item(session, resolved)
    else:
        items = []
        for source, by_day in _source_items(session, window or [resolved]).items():
            item = by_day.get(resolved)
            if item is None:
                item = _item(
                    source,
                    resolved,
                    STATUS_NOT_TRADE_DAY,
                    reasons=["NOT_IN_WINDOW"],
                )
            else:
                item["source"] = source
            items.append(item)
        items.append(_calendar_item(session, resolved))

    return {
        "database": _database_name(session),
        "as_of": resolved,
        "days": days,
        "window_start": window[0] if window else None,
        "window_end": window[-1] if window else None,
        "sources": list(SOURCES),
        "items": items,
    }


def timeline(session: Session, *, source: str, days: int = 120) -> dict[str, Any]:
    """单个来源的逐日时间线（只读）。"""
    days = _validate_days(days)
    source = (source or "").strip()
    if source not in SOURCES:
        raise ValueError(f"未知来源：{source}")
    end = today_cn()
    window = _trade_days(session, end, days)
    if source == "trade_calendar":
        points = [_calendar_item(session, day) for day in window]
    else:
        by_day = _source_items(session, window).get(source, {})
        points = [by_day.get(day) or _item(source, day, STATUS_NOT_COLLECTED) for day in window]
    for point in points:
        point["source"] = source
    return {
        "database": _database_name(session),
        "source": source,
        "days": days,
        "window_start": window[0] if window else None,
        "window_end": window[-1] if window else None,
        "generated_at": datetime.now(),
        "points": points,
    }
