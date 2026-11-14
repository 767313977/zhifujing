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

from datetime import date, datetime, time
from typing import Any

from sqlalchemy import String, case, cast, func, select
from sqlalchemy.exc import OperationalError, SQLAlchemyError
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
from app.services import auction_anomaly as auction_quality
from app.timeutil import now_cn, today_cn

QUALITY_VERSION = "data-quality-v2"

STATUS_VERIFIED = "VERIFIED_SOURCE"
STATUS_PARTIAL = "PARTIAL"
STATUS_UNVERIFIABLE = "UNVERIFIABLE"
STATUS_NOT_COLLECTED = "NOT_COLLECTED"
STATUS_PENDING = "PENDING"
STATUS_NOT_TRADE_DAY = "NOT_TRADE_DAY"
#: 查询/表结构异常：**不能**伪装成「未采集」
STATUS_SOURCE_ERROR = "SOURCE_ERROR"

#: v2 状态名 → 既有字段值（严格映射，不改变旧前端读取的 `status` 语义）
STATUS_V2_MAP = {
    "SOURCE_VERIFIED": STATUS_VERIFIED,
    "SOURCE_PARTIAL": STATUS_PARTIAL,
    "SOURCE_UNVERIFIABLE": STATUS_UNVERIFIABLE,
    "SOURCE_NOT_COLLECTED": STATUS_NOT_COLLECTED,
    "SOURCE_ERROR": STATUS_SOURCE_ERROR,
    "SOURCE_PENDING": STATUS_PENDING,
}

AUCTION_VALID = "VALID"
AUCTION_INCOMPLETE = "INCOMPLETE"
AUCTION_MISSING = "MISSING"
AUCTION_PENDING = "PENDING"
AUCTION_NOT_COLLECTED = "NOT_COLLECTED"
AUCTION_UNVERIFIABLE = "UNVERIFIABLE"

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
        # S5-B：v2 明确把「单来源状态」与「组合视图状态」分开；非组合来源二者相同
        "quality_version": QUALITY_VERSION,
        "source_status": status,
        "combined_view_status": status,
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


def _pool_source_status(
    rows: int,
    latest_any: CollectLog | None,
    latest_ok: CollectLog | None,
    *,
    allow_empty: bool,
    label: str,
) -> tuple[str, list[str], bool]:
    """单个池来源（up / broken / down）的状态。返回 (status, reasons, trusted)。

    - `ok` + 有行 → VERIFIED；
    - broken/down 的 `ok` + 0 行是合法空集；up 的 `ok` + 0 行是异常；
    - 最近一次失败但历史有 ok → PARTIAL（旧快照保留），不抹掉已持久化行；
    - 无日志/从未 ok → UNVERIFIABLE 或 NOT_COLLECTED，绝不当成「空池=可信」。
    """
    # 保留旧 API 的 UP 原因码；BROKEN/DOWN 用前缀区分，避免「up 成功」掩盖它们
    prefix = "" if label == "UP" else f"{label}_"

    def code(text: str) -> str:
        return f"{prefix}{text}"

    if rows == 0:
        if latest_any is None:
            return STATUS_NOT_COLLECTED, [code("POOL_NOT_COLLECTED")], False
        if latest_ok is not None:
            if allow_empty:
                return STATUS_VERIFIED, [], True
            return STATUS_UNVERIFIABLE, [f"{label}_POOL_EMPTY_WITH_OK_LOG"], False
        return (
            STATUS_UNVERIFIABLE,
            [code(f"POOL_STATUS_{(latest_any.status or 'UNKNOWN').upper()}")],
            False,
        )
    if latest_any is None:
        return STATUS_UNVERIFIABLE, [code("POOL_LOG_MISSING_WITH_ROWS")], False
    if (latest_any.status or "").lower() == "ok":
        return STATUS_VERIFIED, [], True
    if latest_ok is not None:
        return STATUS_PARTIAL, [code("LATEST_ATTEMPT_FAILED_SNAPSHOT_RETAINED")], True
    return (
        STATUS_UNVERIFIABLE,
        [code(f"POOL_STATUS_{(latest_any.status or 'UNKNOWN').upper()}")],
        False,
    )


def _combined_pool_status(statuses: list[str]) -> str:
    """「涨停+炸板+跌停」组合视图：任一子来源有缺口都不得 VERIFIED。"""
    if all(status == STATUS_VERIFIED for status in statuses):
        return STATUS_VERIFIED
    if all(status == STATUS_NOT_COLLECTED for status in statuses):
        return STATUS_NOT_COLLECTED
    if any(status == STATUS_UNVERIFIABLE for status in statuses):
        return STATUS_UNVERIFIABLE
    if any(status == STATUS_PARTIAL for status in statuses):
        return STATUS_PARTIAL
    return STATUS_UNVERIFIABLE


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

    valid_days = [
        day
        for day in days
        if counts.get(day, {}).get("up", 0) > 0 and (day, "pool_up") in latest_ok
    ]
    items: dict[date, dict[str, Any]] = {}
    for day in days:
        day_counts = counts.get(day, {})
        up_rows = day_counts.get("up", 0)
        broken_rows = day_counts.get("broken", 0)
        down_rows = day_counts.get("down", 0)
        observed = up_rows + broken_rows + down_rows
        dimensions: dict[str, dict[str, Any]] = {}
        statuses: list[str] = []
        reasons: list[str] = []
        for label, pool_type, pool_rows, allow_empty in (
            ("UP", "up", up_rows, False),
            ("BROKEN", "broken", broken_rows, True),
            ("DOWN", "down", down_rows, True),
        ):
            status, pool_reasons, trusted = _pool_source_status(
                pool_rows,
                latest_any.get((day, f"pool_{pool_type}")),
                latest_ok.get((day, f"pool_{pool_type}")),
                allow_empty=allow_empty,
                label=label,
            )
            statuses.append(status)
            reasons.extend(pool_reasons)
            dimensions[pool_type] = {
                "status": status,
                "rows": pool_rows,
                "completion_proof": status == STATUS_VERIFIED,
                "reason_codes": pool_reasons,
                "latest_attempt": _log_brief(latest_any.get((day, f"pool_{pool_type}"))),
                "latest_valid_snapshot": (
                    max(item for item in valid_days if item <= day)
                    if any(item <= day for item in valid_days)
                    else None
                ),
                "trusted": trusted,
            }

        up_status = dimensions["up"]["status"]
        combined = _combined_pool_status(statuses)
        up_log = latest_any.get((day, "pool_up"))
        items[day] = _item(
            "limit_pool",
            day,
            up_status,
            reasons=reasons,
            observed_rows=observed,
            # 组合视图完全可信才叫「完成证明」；单个 up 池成功不等于三池齐全
            completion_proof=combined == STATUS_VERIFIED,
            latest_observation_date=latest,
            latest_valid_snapshot=dimensions["up"]["latest_valid_snapshot"],
            latest_attempt=_log_brief(up_log),
            note="UP/BROKEN/DOWN 分来源判定；组合视图需三池同时可信",
            extra={
                "up_rows": up_rows,
                "broken_rows": broken_rows,
                "down_rows": down_rows,
                "pool_up": dimensions["up"],
                "pool_broken": dimensions["broken"],
                "pool_down": dimensions["down"],
                "up_completion_proof": dimensions["up"]["completion_proof"],
                "broken_completion_proof": dimensions["broken"]["completion_proof"],
                "down_completion_proof": dimensions["down"]["completion_proof"],
                "combined_view_status": combined,
                "previous_snapshot_available": bool(dimensions["up"]["latest_valid_snapshot"]),
                "current_attempt_failed": bool(
                    up_log is not None and (up_log.status or "").lower() != "ok"
                ),
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
    """竞价质量拆成「0926 终值」与「0931 承接」两个维度。

    复用 `auction_anomaly` 的帧质量规则（`frame_issue_reasons` / `frame_logs_by_day`），
    不再因为日志里出现 `0926` 字样就判定 `VERIFIED_SOURCE`：

    - 0926 有效 + 0931 缺失 → `auction_result_status=VALID`、`post_open_status=MISSING`，
      整体 `PARTIAL`，`completion_proof=false`；
    - 0926 partial → result `INCOMPLETE`，整体不 VERIFIED；
    - 9:31 尚未到达 → post `PENDING`；
    - 有行但没有关键帧日志 → `INCOMPLETE` / `UNVERIFIABLE`，不是验证通过。
    """
    latest = session.scalar(select(func.max(AuctionSnapshot.trade_date)))
    samples_text = cast(AuctionSnapshot.samples, String)
    rows = session.execute(
        select(
            AuctionSnapshot.trade_date,
            func.count(),
            func.count(AuctionSnapshot.post_open_price),
            func.sum(case((samples_text.like('%"0926"%'), 1), else_=0)),
        )
        .where(AuctionSnapshot.trade_date.in_(days))
        .group_by(AuctionSnapshot.trade_date)
    ).all()
    counts = {
        day: (observed or 0, post_count or 0, result_count or 0)
        for day, observed, post_count, result_count in rows
    }
    logs_by_day = auction_quality.frame_logs_by_day(session, days)

    today = today_cn()
    now = now_cn()
    trade_days = set(
        session.scalars(select(TradeCalendar.trade_date).where(TradeCalendar.trade_date.in_(days)))
    )
    post_open_pending = today in trade_days and now.time() < time(9, 31)

    items: dict[date, dict[str, Any]] = {}
    for day in days:
        observed, post_count, result_count = counts.get(day, (0, 0, 0))
        logs = logs_by_day.get(day, {})
        reasons: list[str] = []
        statuses = {str(payload.get("status") or "").lower() for payload in logs.values()}
        all_failed = bool(statuses) and statuses <= {"failed", "skipped"}

        if observed == 0 and not logs:
            result_status = AUCTION_NOT_COLLECTED
            post_status = AUCTION_NOT_COLLECTED
            status = STATUS_NOT_COLLECTED
            reasons.append("AUCTION_NOT_COLLECTED")
        else:
            result_issues = (
                auction_quality.frame_issue_reasons(logs, auction_quality.RESULT_FRAME, "0926")
                if result_count > 0
                else []
            )
            if result_count > 0:
                result_status = AUCTION_INCOMPLETE if result_issues else AUCTION_VALID
                reasons.extend(result_issues)
            elif all_failed:
                result_status = AUCTION_UNVERIFIABLE
                reasons.append("AUCTION_SOURCE_UNAVAILABLE")
            else:
                result_status = AUCTION_MISSING
                reasons.append("AUCTION_0926_MISSING")
                reasons.extend(
                    auction_quality.frame_issue_reasons(logs, auction_quality.RESULT_FRAME, "0926")
                )

            if post_count > 0:
                post_issues = auction_quality.frame_issue_reasons(
                    logs, auction_quality.POST_OPEN_FRAME, "0931"
                )
                post_status = AUCTION_INCOMPLETE if post_issues else AUCTION_VALID
                reasons.extend(post_issues)
            elif post_open_pending and logs.get(auction_quality.POST_OPEN_FRAME) is None:
                post_status = AUCTION_PENDING
                reasons.append("AUCTION_0931_PENDING")
            elif all_failed:
                post_status = AUCTION_UNVERIFIABLE
            else:
                post_status = AUCTION_MISSING
                reasons.append("AUCTION_0931_MISSING")

            if result_status == AUCTION_UNVERIFIABLE and post_status in (
                AUCTION_UNVERIFIABLE,
                AUCTION_MISSING,
                AUCTION_NOT_COLLECTED,
            ):
                status = STATUS_UNVERIFIABLE
            elif result_status == AUCTION_VALID and post_status == AUCTION_VALID:
                status = STATUS_VERIFIED
            else:
                status = STATUS_PARTIAL

        if observed > 0:
            # 质量维度再完整，也不等于「真实交易日竞价实盘验收」已完成
            reasons.append("REAL_AUCTION_DATA_NOT_YET_VERIFIED")

        items[day] = _item(
            "auction_snapshot",
            day,
            status,
            reasons=reasons,
            observed_rows=observed,
            completion_proof=status == STATUS_VERIFIED,
            latest_observation_date=latest,
            latest_valid_snapshot=day if status == STATUS_VERIFIED else None,
            latest_attempt=(
                {
                    "status": (logs.get(auction_quality.RESULT_FRAME) or {}).get(
                        "last_attempt_status"
                    ),
                    "frame": auction_quality.RESULT_FRAME,
                    "created_at": None,
                }
                if logs.get(auction_quality.RESULT_FRAME)
                else None
            ),
            note=NOTE_AUCTION,
            extra={
                "frames": sorted(logs),
                "auction_result_status": result_status,
                "post_open_status": post_status,
                "has_0926": result_count > 0,
                "has_0931": post_count > 0,
                "result_count": result_count,
                "post_open_count": post_count,
            },
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


def _safe_source(
    source: str, fn, session: Session, days: list[date]
) -> dict[date, dict[str, Any]]:
    """单表缺失/损坏只影响这个来源，不拖垮整页；**不建表、不修库**。

    `OperationalError` 等查询异常 → `SOURCE_ERROR`，并给出 `DB_SCHEMA_UNAVAILABLE`
    / `DB_QUERY_FAILED` 安全原因码；**不能**伪装成 `NOT_COLLECTED`，也不向前端
    暴露 SQL、磁盘路径或连接串。
    """
    try:
        return fn(session, days)
    except (OperationalError, SQLAlchemyError) as exc:
        message = str(getattr(exc, "orig", exc)).lower()
        schema_missing = "no such table" in message or "no such column" in message
        return {
            day: _item(
                source,
                day,
                STATUS_SOURCE_ERROR,
                reasons=[
                    "DB_SCHEMA_UNAVAILABLE" if schema_missing else "DB_QUERY_FAILED"
                ],
                note="数据源查询异常，无法断言未采集",
            )
            for day in days
        }


def _source_items(
    session: Session, days: list[date], sources: list[str] | None = None
) -> dict[str, dict[date, dict[str, Any]]]:
    """只计算需要的来源：timeline 查单个来源时不该把五张表全平扫一遍。"""
    builders = {
        "stock_daily": _stock_daily_items,
        "limit_pool": _limit_pool_items,
        "sector_member": _sector_member_items,
        "market_sentiment": _market_sentiment_items,
        "auction_snapshot": _auction_items,
    }
    selected = sources or list(builders)
    return {
        name: _safe_source(name, builders[name], session, days)
        for name in selected
        if name in builders
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
        # overview 只看目标日：窗口只用于确定「非交易日 / 未来日」，不需要逐日扫描
        for source, by_day in _source_items(session, [resolved]).items():
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
        "quality_version": QUALITY_VERSION,
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
        # 只算被选中的来源，避免 timeline 顺手把全市场日线也 group by 一遍
        by_day = _source_items(session, window, sources=[source]).get(source, {})
        points = [by_day.get(day) or _item(source, day, STATUS_NOT_COLLECTED) for day in window]
    for point in points:
        point["source"] = source
    return {
        "database": _database_name(session),
        "quality_version": QUALITY_VERSION,
        "source": source,
        "days": days,
        "window_start": window[0] if window else None,
        "window_end": window[-1] if window else None,
        "generated_at": datetime.now(),
        "points": points,
    }
