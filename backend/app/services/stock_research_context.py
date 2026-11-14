"""个股「观察日研究依据」只读解释链（S3-C → S4-A v2 → S5-A 时点证据 v3）。

目标：把四件事**结构化且互斥**地拆开，而不是只靠文案区分：

1. `at_cutoff_known`：有可核验的时间证明、在 `cutoff_at` 之前本站已取得，
   且采集质量满足该来源规则的字段；
2. `unknown_or_later`：`LATER / SAME_DAY_UNKNOWN / POST_CLOSE_UNKNOWN / UNKNOWN /
   END_OF_DAY_FACT` 等无法证明「观察日 cutoff 前已知」的字段；
3. `end_of_day_fact`：`stock_daily` / `market_sentiment` 没有首次入库时间，
   只能证明「D 日收盘后的日终事实」，**不能**证明 15:00 当时本站已知；
4. `later_outcome`：D+1 或更晚的后验结果，只供复盘，绝不反写 D 日依据。

S5-A 的时点口径（v3）：

- `cutoff="CLOSE"` 在系统内**明确对应**观察日 15:00（Asia/Shanghai）；
  `collect_log.created_at` 是 naive 本地时间（`models._now()` 约定 Asia/Shanghai），
  只有明确信任该约定时才按 +08:00 解释，否则降级 `UNKNOWN`；
- 日志在 15:00 之后（含 15:01 / 22:00）→ `POST_CLOSE_UNKNOWN`，不进已知桶；
- 日志在 D+1 → `LATER`；
- `source_published_at` 只有真实来源可核验时才填，当前一律 null，不虚构。

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
from datetime import date, datetime, time
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
from app.timeutil import CST, today_cn

CODE_PATTERN = re.compile(r"^\d{6}$")

#: 时点协议版本。字段语义变化必须升版本，避免旧前端把兼容子集当成完整证据。
PIT_VERSION = "research-evidence-v3"
#: 向后兼容别名（旧代码/旧测试仍按 EVIDENCE_VERSION 读取）。
EVIDENCE_VERSION = PIT_VERSION

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
#: `stock_daily` / `market_sentiment` 没有首次入库时间：只能证明「D 日收盘后的日终事实」
KNOWN_END_OF_DAY_FACT = "END_OF_DAY_FACT"

BUCKET_KNOWN = "AT_CUTOFF_KNOWN"
BUCKET_UNKNOWN = "UNKNOWN_OR_LATER"
BUCKET_FORWARD = "FORWARD_OUTCOME"
TIME_BUCKETS = (BUCKET_KNOWN, BUCKET_UNKNOWN, BUCKET_FORWARD)

#: 每个桶允许的 `known_by`。三桶互斥且并集覆盖全部证据。
BUCKET_ALLOWED_KNOWN_BY: dict[str, frozenset[str]] = {
    BUCKET_KNOWN: frozenset({KNOWN_CLOSE, KNOWN_PRIOR_DAY}),
    BUCKET_UNKNOWN: frozenset(
        {
            KNOWN_SAME_DAY_UNKNOWN,
            KNOWN_LATER,
            KNOWN_POST_CLOSE,
            KNOWN_UNKNOWN,
            KNOWN_END_OF_DAY_FACT,
        }
    ),
    BUCKET_FORWARD: frozenset({KNOWN_FORWARD}),
}

CUTOFF_CLOSE = "CLOSE"
#: 本批次只正式支持收盘时点。09:25 竞价时点证据不足，拒绝而不是假装同口径。
SUPPORTED_CUTOFFS = (CUTOFF_CLOSE,)
#: `CLOSE` 在系统内的**明确时刻**：观察日 15:00 Asia/Shanghai。
CUTOFF_CLOCK: dict[str, time] = {CUTOFF_CLOSE: time(15, 0)}
CUTOFF_LABELS = {CUTOFF_CLOSE: "收盘截止 15:00 Asia/Shanghai（含 15:00 整）"}

POOL_STATUS_UP = "UP"
POOL_STATUS_BROKEN = "BROKEN"
POOL_STATUS_DOWN = "DOWN"
POOL_STATUS_NOT_IN_POOL = "NOT_IN_POOL"
POOL_STATUS_CONFLICT = "POOL_CONFLICT"
POOL_STATUS_UNKNOWN = "UNKNOWN"

NOTES = [
    "「观察日已知」严格按 cutoff=CLOSE = 15:00 Asia/Shanghai 解释；15:01 及以后才取到的日志进「后得/未知」。",
    "stock_daily / market_sentiment 没有首次入库时间，只列为「日终事实」，不能证明 15:00 当时本站已知。",
    "题材快照的 fetched_at 是本系统写入时间，只能证明「本站何时取到」，不能证明上游盘前已发布。",
    "涨停天梯（stock_concept）没有可审计的上游发布时间，一律归入「不知道 / 观察日之后才取到」。",
    "D+1 结果只供复盘，不反写 D 日依据，也不进入 C0 或任何正式信号。",
]


# ---------------------------------------------------------------- 时点判定（纯函数）


def _to_cn(moment: datetime | None, *, naive_timezone_trusted: bool) -> datetime | None:
    """统一到 Asia/Shanghai。

    naive 时间只有在本项目 `models._now()` 的 Asia/Shanghai 约定被明确信任时才解释；
    否则返回 None（调用方降级 UNKNOWN），不把本机系统时区强加给历史日志。
    """
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=CST) if naive_timezone_trusted else None
    return moment.astimezone(CST)


def resolve_cutoff_at(observation: date, cutoff: str = CUTOFF_CLOSE) -> datetime:
    """观察日的 cutoff 时刻（带 +08:00）。不支持/非法 cutoff 直接拒绝。"""
    key = (cutoff or CUTOFF_CLOSE).upper()
    if key not in SUPPORTED_CUTOFFS:
        raise ValueError(f"暂不支持的 cutoff：{cutoff}（当前只支持 CLOSE）")
    return datetime.combine(observation, CUTOFF_CLOCK[key], tzinfo=CST)


def evaluate_known_time(
    *,
    observed_at: datetime | None,
    cutoff_at: datetime,
    published_at: datetime | None = None,
    data_ready: bool = True,
    naive_timezone_trusted: bool = True,
) -> dict[str, Any]:
    """判定一条证据在 cutoff 时点是否「已取得」（纯函数，不读库）。

    返回 `{known_by, time_bucket, known_at, reason_code}`：

    - 采集质量/完成性不成立 → `UNKNOWN`（时间再早也不算已知）；
    - 缺 `observed_at` → `UNKNOWN`；
    - naive 且不信任时区约定 → `UNKNOWN`（`NAIVE_TIMESTAMP_NO_TRUSTED_TZ`）；
    - 最早可用时刻 = max(observed_at, published_at)；`<= cutoff_at` → `CLOSE`；
    - 同日但在 cutoff 之后 → `POST_CLOSE_UNKNOWN`；
    - 更晚日期 → `LATER`。

    「市场客观事件发生」与「系统已收到可用于研究的数据」始终分开：本函数只回答后者。
    """
    if cutoff_at.tzinfo is None:
        raise ValueError("cutoff_at 必须带时区（Asia/Shanghai）")
    cutoff_cn = cutoff_at.astimezone(CST)

    def result(known_by: str, reason: str | None, known_at: datetime | None = None):
        bucket = classify_evidence_time(known_by)
        return {
            "known_by": known_by,
            "time_bucket": bucket,
            "known_at": known_at,
            "reason_code": reason,
        }

    if not data_ready:
        return result(KNOWN_UNKNOWN, "SOURCE_NOT_READY")
    observed_cn = _to_cn(observed_at, naive_timezone_trusted=naive_timezone_trusted)
    if observed_cn is None:
        reason = (
            "OBSERVED_AT_MISSING"
            if observed_at is None
            else "NAIVE_TIMESTAMP_NO_TRUSTED_TZ"
        )
        return result(KNOWN_UNKNOWN, reason)
    published_cn = _to_cn(published_at, naive_timezone_trusted=naive_timezone_trusted)
    if published_at is not None and published_cn is None:
        return result(KNOWN_UNKNOWN, "NAIVE_TIMESTAMP_NO_TRUSTED_TZ")

    known_at = observed_cn if published_cn is None else max(observed_cn, published_cn)
    if known_at <= cutoff_cn:
        return result(KNOWN_CLOSE, None, known_at)
    if known_at.date() == cutoff_cn.date():
        return result(KNOWN_POST_CLOSE, "AFTER_CUTOFF_SAME_DAY", known_at)
    return result(KNOWN_LATER, "AFTER_CUTOFF_LATER_DAY", known_at)


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


def _log_known_by(
    log: CollectLog | None,
    observation: date,
    *,
    cutoff: str = CUTOFF_CLOSE,
    data_ready: bool = True,
    naive_timezone_trusted: bool = True,
) -> str:
    """采集日志只能证明本站何时记录，不等于上游发布时间。

    v3：`cutoff=CLOSE` 对应观察日 15:00。日志在 15:00 之后 → `POST_CLOSE_UNKNOWN`
    （不再是「收盘可知」）；D+1 → `LATER`；没有日志 → `UNKNOWN`（不猜）。
    """
    return evaluate_known_time(
        observed_at=(log.created_at if log is not None else None),
        cutoff_at=resolve_cutoff_at(observation, cutoff),
        data_ready=data_ready,
        naive_timezone_trusted=naive_timezone_trusted,
    )["known_by"]


def _membership_pit(
    membership: list[str],
    logs: dict[str, CollectLog | None],
    pits: dict[str, dict[str, Any]],
) -> tuple[str, datetime | None, str | None]:
    """「这只票在哪些池」这一断言的时点。

    该断言依赖**所有**与之相关的池来源：只要有一个池日志缺失/失败/晚于 cutoff，
    整体就降级，不能拿另一个池的早日志替它作证。
    - 有 membership → 只看成员池；
    - 无 membership（NOT_IN_POOL 断言）→ 需要三个池都可证明。
    """
    relevant = [pool_type for pool_type in ("up", "broken", "down") if pool_type in membership]
    if not relevant:
        relevant = ["up", "broken", "down"]
    observed: datetime | None = None
    for pool_type in relevant:
        pit = pits.get(pool_type) or {
            "known_by": KNOWN_UNKNOWN,
            "time_bucket": BUCKET_UNKNOWN,
            "reason_code": "POOL_LOG_MISSING",
        }
        if pit["time_bucket"] != BUCKET_KNOWN:
            log = logs.get(pool_type)
            return pit["known_by"], (log.created_at if log is not None else None), pit["reason_code"]
        log = logs.get(pool_type)
        moment = log.created_at if log is not None else None
        if observed is None or (moment is not None and moment > observed):
            observed = moment
    return KNOWN_CLOSE, observed, None


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
    cutoff_at = resolve_cutoff_at(resolved, cutoff)
    cutoff_at_iso = cutoff_at.isoformat()

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
    pool_logs = {"up": up_log, "broken": broken_log, "down": down_log}
    pool_pits = {
        "up": evaluate_known_time(
            observed_at=(up_log.created_at if up_log else None),
            cutoff_at=cutoff_at,
            data_ready=up_trusted,
        ),
        "broken": evaluate_known_time(
            observed_at=(broken_log.created_at if broken_log else None),
            cutoff_at=cutoff_at,
            data_ready=broken_trusted,
        ),
        "down": evaluate_known_time(
            observed_at=(down_log.created_at if down_log else None),
            cutoff_at=cutoff_at,
            data_ready=down_trusted,
        ),
    }
    membership_known_by, membership_observed_at, membership_reason = _membership_pit(
        pool_types, pool_logs, pool_pits
    )
    up_pit = pool_pits["up"]["time_bucket"] == BUCKET_KNOWN
    broken_pit = pool_pits["broken"]["time_bucket"] == BUCKET_KNOWN
    down_pit = pool_pits["down"]["time_bucket"] == BUCKET_KNOWN

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
        # stock_daily / market_sentiment 没有首次入库时间 → 不进「已知」，见 end_of_day_fact
        "daily": None,
        # 只有「观察日之前已取到」的题材能进已知；同日/次日取到一律不进
        "themes": known_themes,
        # stock_concept 没有可审计的发布时间 → 已知集合里永远为空
        "concepts": [],
        "sentiment": None,
    }

    end_of_day_fact = {
        "daily": (
            {
                "trade_date": resolved,
                "close": daily.close,
                "pct_chg": daily.pct_chg,
                "known_by": KNOWN_END_OF_DAY_FACT,
            }
            if daily is not None
            else None
        ),
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
        "known_by": KNOWN_END_OF_DAY_FACT,
        "reason_code": "END_OF_DAY_FACT_NOT_AT_CUTOFF",
        "note": (
            "stock_daily / market_sentiment 没有首次入库时间，只能证明「D 日收盘后的日终事实」，"
            "不能证明 15:00 当时本站已知，也不能当作可交易的盘中信号。"
        ),
    }

    unknown_reasons: list[str] = []
    if unknown_themes:
        unknown_reasons.append("THEME_PIT_UNKNOWN")
    if concepts:
        unknown_reasons.append("CONCEPT_POST_CLOSE_UNKNOWN")
    if daily is not None or sentiment is not None:
        unknown_reasons.append("END_OF_DAY_FACT_NOT_AT_CUTOFF")
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
    if up_quality == STATUS_VERIFIED and not up_pit:
        reasons.append("POOL_NOT_KNOWN_AT_CUTOFF")
    if pool_conflict:
        reasons.append("POOL_CONFLICT")
    if not themes and not concepts:
        reasons.append("NO_THEME_SNAPSHOT")
    if unknown_themes:
        reasons.append("THEME_PIT_UNKNOWN")
    if concepts:
        reasons.append("CONCEPT_POST_CLOSE_UNKNOWN")
    if daily is not None or sentiment is not None:
        reasons.append("END_OF_DAY_FACT_NOT_AT_CUTOFF")
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
        known_at=None,
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
                "known_at": known_at,
                "cutoff_at": cutoff_at_iso,
                "known_by": known_by,
                # v3 起时间与质量分开：evidence_quality 是来源完成性，不是时点
                "evidence_quality": quality,
                "quality": quality,
                "reason_code": reason,
                "is_forward_known": bucket == BUCKET_FORWARD,
                "time_bucket": bucket,
                "summary": summary,
            }
        )

    pool_reason = membership_reason
    if pool_conflict:
        pool_reason = "POOL_CONFLICT"
    elif membership_known_by != KNOWN_CLOSE and pool_reason == "SOURCE_NOT_READY":
        for pool_type in pool_types or ["up", "broken", "down"]:
            if pool_pits[pool_type]["time_bucket"] != BUCKET_KNOWN:
                pool_reason = {
                    "up": up_reason,
                    "broken": broken_reason,
                    "down": down_reason,
                }[pool_type]
                break
    if membership:
        labels = "、".join(pool_types)
        summary = (
            f"当日观察到 {labels}；判定 {limit_status}；涨停池质量 {up_quality}"
            if not pool_conflict
            else f"同一股票同时出现在 {labels} 池，不能按优先级择一"
        )
    else:
        summary = "当日没有该股的涨停/炸板/跌停记录"
    add(
        "观察日池记录",
        "limit_pool",
        resolved,
        membership_observed_at,
        membership_known_by,
        up_quality,
        pool_reason,
        summary,
        observed_at=membership_observed_at,
        known_at=membership_observed_at,
    )

    if daily is not None:
        add(
            "观察日日线（日终事实）",
            "stock_daily",
            resolved,
            None,
            KNOWN_END_OF_DAY_FACT,
            STATUS_VERIFIED,
            "END_OF_DAY_FACT_NOT_AT_CUTOFF",
            f"收盘 {daily.close}，涨跌幅 {daily.pct_chg}；无首次入库时间，只作复盘",
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
            "观察日市场情绪（日终事实）",
            "market_sentiment",
            resolved,
            None,
            KNOWN_END_OF_DAY_FACT,
            STATUS_VERIFIED,
            "END_OF_DAY_FACT_NOT_AT_CUTOFF",
            f"涨停 {sentiment.limit_up_count} 家，最高 {sentiment.max_consecutive} 板；只作复盘",
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
        "cutoff_at": cutoff_at_iso,
        "cutoff_label": CUTOFF_LABELS[cutoff],
        "timezone": "Asia/Shanghai",
        "evidence_version": EVIDENCE_VERSION,
        "pit_version": PIT_VERSION,
        "data_as_of": data_as_of,
        "historical_signal_eligible": False,
        "quality": {"status": status, "reasons": _dedupe(reasons)},
        "at_cutoff_known": at_cutoff_known,
        "end_of_day_fact": end_of_day_fact,
        "unknown_or_later": unknown_or_later,
        "later_outcome": later_outcome,
        "evidence": evidence,
        "evidence_counts": counts,
        # 兼容旧消费者：已知子集，不含 LATER / 同日未知 / 盘后未知 / 日终事实
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
