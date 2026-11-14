"""个股「观察日研究依据」只读解释链（S3-C → S4-A 时点证据 v2）。

目标：把三件事**结构化且互斥**地拆开，而不是只靠文案区分：

1. `at_cutoff_known`：有充分时间证明、在选定 cutoff（当前只支持 `CLOSE`）已知，
   且采集质量满足该来源规则的字段；
2. `unknown_or_later`：`LATER / SAME_DAY_UNKNOWN / POST_CLOSE_UNKNOWN / UNKNOWN`
   等无法证明「观察日已知」的字段；
3. `later_outcome`：D+1 或更晚的后验结果，只供复盘，绝不反写 D 日依据。

边界：
- 只读本地库（limit_pool / stock_daily / market_sentiment / sector_member /
  stock_concept / collect_log / trade_calendar / stock_basic）；
- 不调用 `syncStock`、不抓公告/题材/龙虎榜/外部行情，不写库；
- `SectorMember.fetched_at` 是本系统写入时间，只能证明「我们何时取到」；
  同日取到一律 `SAME_DAY_UNKNOWN`，不猜盘前已知；
- `StockConcept` 没有可审计的上游发布时间，永远进 `unknown_or_later`；
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

#: 时点协议版本。字段语义变化必须升版本，避免旧前端把兼容子集当成完整证据。
EVIDENCE_VERSION = "research-evidence-v2"

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
KNOWN_UNKNOWN = "UNKNOWN"

BUCKET_KNOWN = "AT_CUTOFF_KNOWN"
BUCKET_UNKNOWN = "UNKNOWN_OR_LATER"
BUCKET_FORWARD = "FORWARD_OUTCOME"
TIME_BUCKETS = (BUCKET_KNOWN, BUCKET_UNKNOWN, BUCKET_FORWARD)

#: 每个桶允许的 `known_by`。三桶互斥且并集覆盖全部证据。
BUCKET_ALLOWED_KNOWN_BY: dict[str, frozenset[str]] = {
    BUCKET_KNOWN: frozenset({KNOWN_CLOSE, KNOWN_PRIOR_DAY}),
    BUCKET_UNKNOWN: frozenset(
        {KNOWN_SAME_DAY_UNKNOWN, KNOWN_LATER, KNOWN_POST_CLOSE, KNOWN_UNKNOWN}
    ),
    BUCKET_FORWARD: frozenset({KNOWN_FORWARD}),
}

CUTOFF_CLOSE = "CLOSE"
#: 本批次只正式支持收盘时点。09:25 竞价时点证据不足，拒绝而不是假装同口径。
SUPPORTED_CUTOFFS = (CUTOFF_CLOSE,)

POOL_STATUS_UP = "UP"
POOL_STATUS_BROKEN = "BROKEN"
POOL_STATUS_DOWN = "DOWN"
POOL_STATUS_NOT_IN_POOL = "NOT_IN_POOL"
POOL_STATUS_CONFLICT = "POOL_CONFLICT"
POOL_STATUS_UNKNOWN = "UNKNOWN"

NOTES = [
    "「观察日已知」按收盘时点解释；09:25 竞价时点证据不足，本接口不提供。",
    "题材快照的 fetched_at 是本系统写入时间，只能证明「本站何时取到」，不能证明上游盘前已发布。",
    "涨停天梯（stock_concept）没有可审计的上游发布时间，一律归入「不知道 / 观察日之后才取到」。",
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
        return KNOWN_UNKNOWN
    day = fetched_at.date() if hasattr(fetched_at, "date") else None
    if day is None:
        return KNOWN_UNKNOWN
    if day < observation:
        return KNOWN_PRIOR_DAY
    if day > observation:
        return KNOWN_LATER
    return KNOWN_SAME_DAY_UNKNOWN


def _log_known_by(log: CollectLog | None, observation: date) -> str:
    """采集日志只能证明本站何时记录，不等于上游发布时间。

    日志在观察日当天（含收盘后）→ 按收盘时点可获知；
    日志在观察日之后 → LATER；没有日志 → UNKNOWN（不猜）。
    """
    if log is None or log.created_at is None:
        return KNOWN_UNKNOWN
    return KNOWN_CLOSE if log.created_at.date() <= observation else KNOWN_LATER


def classify_evidence_time(known_by: str) -> str:
    """把 `known_by` 归入唯一的时间桶。未知取值直接报错，不静默塞进已知。"""
    if known_by in BUCKET_ALLOWED_KNOWN_BY[BUCKET_KNOWN]:
        return BUCKET_KNOWN
    if known_by in BUCKET_ALLOWED_KNOWN_BY[BUCKET_UNKNOWN]:
        return BUCKET_UNKNOWN
    if known_by in BUCKET_ALLOWED_KNOWN_BY[BUCKET_FORWARD]:
        return BUCKET_FORWARD
    raise ValueError(f"未知的 known_by：{known_by!r}")


def build_time_buckets(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """按 `time_bucket` 分组，返回三桶与计数（纯函数）。"""
    buckets: dict[str, list[dict[str, Any]]] = {name: [] for name in TIME_BUCKETS}
    for item in evidence:
        bucket = item.get("time_bucket")
        if bucket not in buckets:
            raise ValueError(f"证据缺少合法 time_bucket：{bucket!r}")
        buckets[bucket].append(item)
    return {
        "buckets": buckets,
        "counts": {name: len(buckets[name]) for name in TIME_BUCKETS},
        "total": len(evidence),
    }


def validate_time_bucket_invariants(
    evidence: list[dict[str, Any]], *, observation_date: date | None = None
) -> dict[str, int]:
    """机器可断言的不变量：三桶互斥、并集守恒、`is_forward_known` 与桶一致。"""
    grouped = build_time_buckets(evidence)
    seen: set[int] = set()
    for bucket, items in grouped["buckets"].items():
        allowed = BUCKET_ALLOWED_KNOWN_BY[bucket]
        for item in items:
            marker = id(item)
            if marker in seen:
                raise RuntimeError("同一条证据被归入多个时间桶")
            seen.add(marker)
            if item.get("known_by") not in allowed:
                raise RuntimeError(
                    f"时间桶 {bucket} 出现不允许的 known_by={item.get('known_by')!r}"
                )
            if bool(item.get("is_forward_known")) != (bucket == BUCKET_FORWARD):
                raise RuntimeError("is_forward_known 与 time_bucket 不一致")
            source_day = item.get("source_trade_date")
            if bucket == BUCKET_FORWARD and observation_date is not None and source_day:
                if source_day <= observation_date:
                    raise RuntimeError("后验桶里出现了不晚于观察日的来源日期")
            if bucket == BUCKET_KNOWN and observation_date is not None and source_day:
                if source_day > observation_date:
                    raise RuntimeError("已知桶里出现了观察日之后的来源日期")
    if grouped["counts"][BUCKET_KNOWN] + grouped["counts"][BUCKET_UNKNOWN] + grouped["counts"][
        BUCKET_FORWARD
    ] != len(evidence):
        raise RuntimeError("三桶计数不守恒")
    return grouped["counts"]


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
    session: Session,
    *,
    code: str,
    as_of: date | None = None,
    cutoff: str = CUTOFF_CLOSE,
) -> dict[str, Any]:
    """构建一只股票在观察日的「已知 / 不知道或后得 / 后验」三段式依据。"""
    if not CODE_PATTERN.match(code or ""):
        raise ValueError("股票代码必须是 6 位数字")
    cutoff = (cutoff or CUTOFF_CLOSE).upper()
    if cutoff not in SUPPORTED_CUTOFFS:
        raise ValueError(f"暂不支持的 cutoff：{cutoff}（当前只支持 CLOSE）")
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

    # ---------------- 池状态：冲突优先，不使用 if/elif 优先级掩盖冲突
    pool_types = sorted(membership)
    pool_conflict = len(pool_types) > 1
    up_pit = _log_known_by(up_log, resolved) in BUCKET_ALLOWED_KNOWN_BY[BUCKET_KNOWN]
    broken_pit = _log_known_by(broken_log, resolved) in BUCKET_ALLOWED_KNOWN_BY[BUCKET_KNOWN]
    down_pit = _log_known_by(down_log, resolved) in BUCKET_ALLOWED_KNOWN_BY[BUCKET_KNOWN]

    limit_status = POOL_STATUS_UNKNOWN
    board_height = None
    if pool_conflict:
        limit_status = POOL_STATUS_CONFLICT
    elif "up" in membership and up_trusted and up_pit:
        limit_status = POOL_STATUS_UP
        board_height = membership["up"].consecutive
    elif "broken" in membership and broken_trusted and broken_pit:
        limit_status = POOL_STATUS_BROKEN
    elif "down" in membership and down_trusted and down_pit:
        limit_status = POOL_STATUS_DOWN
    elif up_trusted and broken_trusted and down_trusted and up_pit and broken_pit and down_pit:
        limit_status = POOL_STATUS_NOT_IN_POOL

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

    known_themes = [item for item in themes if item["known_by"] == KNOWN_PRIOR_DAY]
    unknown_themes = [item for item in themes if item["known_by"] != KNOWN_PRIOR_DAY]

    at_cutoff_known = {
        "previous_close_date": previous,
        "pool_status": limit_status,
        "board_height": board_height,
        "observed_pool_membership": pool_types,
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
        # 只有「观察日之前已取到」的题材能进已知；同日/次日取到一律不进
        "themes": known_themes,
        # stock_concept 没有可审计的发布时间 → 已知集合里永远为空
        "concepts": [],
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

    unknown_reasons: list[str] = []
    if unknown_themes:
        unknown_reasons.append("THEME_PIT_UNKNOWN")
    if concepts:
        unknown_reasons.append("CONCEPT_POST_CLOSE_UNKNOWN")
    unknown_or_later = {
        "themes": unknown_themes,
        "concepts": concepts,
        "reasons": unknown_reasons,
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
    if pool_conflict:
        reasons.append("POOL_CONFLICT")
    if not themes and not concepts:
        reasons.append("NO_THEME_SNAPSHOT")
    if unknown_themes:
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

    # ---------------- 证据链（每条只进一个桶）
    evidence: list[dict[str, Any]] = []

    def add(
        claim: str,
        table: str,
        source_day: date | None,
        fetched_at,
        known_by: str,
        quality: str,
        reason: str | None,
        summary: str,
        *,
        observed_at=None,
    ) -> None:
        bucket = classify_evidence_time(known_by)
        evidence.append(
            {
                "claim": claim,
                "source_table": table,
                "source_trade_date": source_day,
                "fetched_at": fetched_at,
                # 上游首次发布时间：当前数据源拿不到，一律 null，不虚构
                "source_published_at": None,
                "observed_at": observed_at,
                "known_by": known_by,
                "quality": quality,
                "reason_code": reason,
                "is_forward_known": bucket == BUCKET_FORWARD,
                "time_bucket": bucket,
                "summary": summary,
            }
        )

    pool_known_by = _log_known_by(up_log, resolved)
    if membership:
        labels = "、".join(pool_types)
        summary = (
            f"当日观察到 {labels}；判定 {limit_status}；涨停池质量 {up_quality}"
            if not pool_conflict
            else f"同一股票同时出现在 {labels} 池，不能按优先级择一"
        )
        add(
            "观察日池记录",
            "limit_pool",
            resolved,
            up_log.created_at if up_log else None,
            pool_known_by,
            up_quality,
            "POOL_CONFLICT" if pool_conflict else up_reason,
            summary,
            observed_at=up_log.created_at if up_log else None,
        )
    else:
        add(
            "观察日池记录",
            "limit_pool",
            resolved,
            up_log.created_at if up_log else None,
            pool_known_by,
            up_quality,
            up_reason,
            "当日没有该股的涨停/炸板/跌停记录",
            observed_at=up_log.created_at if up_log else None,
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
            f"收盘 {daily.close}，涨跌幅 {daily.pct_chg}",
        )
    else:
        add(
            "观察日日线",
            "stock_daily",
            resolved,
            None,
            KNOWN_UNKNOWN,
            STATUS_NOT_COLLECTED,
            "DAILY_MISSING",
            "观察日没有本地日线，无法证明收盘价",
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
            f"涨停 {sentiment.limit_up_count} 家，最高 {sentiment.max_consecutive} 板",
        )

    # 题材逐条入证据：同日取到与次日取到不会被合并成一条
    for item in themes:
        add(
            "题材归属快照",
            "sector_member",
            resolved,
            item["fetched_at"],
            item["known_by"],
            STATUS_PARTIAL,
            "THEME_PIT_UNKNOWN",
            f"{item['name']}（{item['taxonomy']}）",
            observed_at=item["fetched_at"],
        )
    for item in concepts:
        add(
            "涨停天梯题材",
            "stock_concept",
            resolved,
            None,
            KNOWN_POST_CLOSE,
            STATUS_PARTIAL,
            "CONCEPT_POST_CLOSE_UNKNOWN",
            item["name"],
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
            (
                f"D+1 涨跌幅 {outcome_pct}，收盘 {outcome_close}"
                if outcome_pct is not None
                else "D+1 行情未到齐或不可验证"
            ),
        )

    counts = validate_time_bucket_invariants(evidence, observation_date=resolved)

    # 旧字段兼容：只保留已知子集，不再承载 LATER 题材（S4 迁移说明见模块顶部）
    as_known = {
        **at_cutoff_known,
        "limit_status": at_cutoff_known["pool_status"],
        "schema_version": EVIDENCE_VERSION,
        "subset_of": "at_cutoff_known",
    }
    as_known.pop("pool_status", None)

    return {
        "code": code,
        "name": basic.name,
        "requested_as_of": as_of,
        "resolved_trade_date": resolved,
        "observation_date": resolved,
        "cutoff": cutoff,
        "evidence_version": EVIDENCE_VERSION,
        "data_as_of": data_as_of,
        "historical_signal_eligible": False,
        "quality": {"status": status, "reasons": _dedupe(reasons)},
        "at_cutoff_known": at_cutoff_known,
        "unknown_or_later": unknown_or_later,
        "later_outcome": later_outcome,
        "evidence": evidence,
        "evidence_counts": counts,
        # 兼容旧消费者：已知子集，不含 LATER / 同日未知 / 盘后未知
        "as_known_on_day": as_known,
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
