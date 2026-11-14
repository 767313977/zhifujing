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

from app.config import get_settings
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

#: 三池来源的固定顺序（质量、时点、证据都按这个顺序输出）
POOL_TYPES = ("up", "broken", "down")

#: naive 时间戳策略：显式声明，不依赖函数默认值（S5 审查整改 P1-02）。
NAIVE_POLICY_CST_ASSUMED = "CST_ASSUMED"
NAIVE_POLICY_UNTRUSTED = "UNTRUSTED"
NAIVE_POLICY_NOTES = {
    NAIVE_POLICY_CST_ASSUMED: (
        "历史 naive 时间戳按 Asia/Shanghai 解释（显式开启；用于能证明历史写入环境就是 CST 的部署）"
    ),
    NAIVE_POLICY_UNTRUSTED: (
        "历史 naive 时间戳不按任何时区解释：无法证明来源时区时一律降级 UNKNOWN，不猜 +08:00"
    ),
}
#: 新写入时间由 `models._now()` 生成：先取 +08:00，再去 tzinfo 写进 naive 列。
WRITE_PATH_TIME_SOURCE = "app.models._now() = timeutil.now_cn() 去 tzinfo（确定性 CST naive）"


def resolve_naive_timezone_trusted(override: bool | None = None) -> bool:
    """解析「历史 naive 时间是否按 +08:00 解释」。

    `override` 只给测试/调用方显式指定；否则读配置
    `research_naive_timestamps_trusted`（默认 false）。**没有隐式默认 true**。
    """
    if override is not None:
        return bool(override)
    return bool(get_settings().research_naive_timestamps_trusted)

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
    naive_timezone_trusted: bool,
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


def _theme_known_by(
    fetched_at, observation: date, *, naive_timezone_trusted: bool
) -> str:
    """题材快照只能证明「本站何时取到」，且仍需显式时区策略。"""
    moment = _to_cn(fetched_at, naive_timezone_trusted=naive_timezone_trusted)
    if moment is None:
        return KNOWN_UNKNOWN
    day = moment.date()
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
    naive_timezone_trusted: bool,
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


def _resolve_pool_evidence(
    *,
    membership: dict[str, Any],
    quality: dict[str, str],
    trusted: dict[str, bool],
    pits: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """解析「这只票在哪些池」这一断言在截止时点的状态（R2-P1-01）。

    判定顺序固定为「先质量、后时间」，且**完整性必须检查全部三池**：

    - `observed_member_types`：数据库里确实有该股记录的池；
    - `required_sources`：协议要求用于证明池状态完整性的全部来源（固定三池），
      **不得**由成员行反推，否则「只出现在 up」会把 broken/down 的未知当成已知为空；
    - `known_sources`：来源可信且 `known_at <= cutoff`；
    - `pending_sources`：未能在 cutoff 前证明的 required sources；
    - 已知区只放 `known_membership = observed_member_types ∩ known_sources`；
    - `POOL_CONFLICT` 只在**构成冲突的全部来源**都已证明时成立；
    - `NOT_IN_POOL` 只在三个来源全部证明且无成员时成立；
    - 单边正向成员（如已证明 up）可以显示 `UP`，但同时标记 `pool_scope_complete=false`，
      不推断其余池为空；
    - 未证明的来源/成员/潜在冲突只进 `unknown`，但已证明的成员不得被描述成未证明。
    """
    observed_member_types = sorted(
        pool_type for pool_type in POOL_TYPES if pool_type in membership
    )
    required_sources = list(POOL_TYPES)
    known_sources = sorted(
        pool_type
        for pool_type in required_sources
        if trusted[pool_type] and pits[pool_type]["time_bucket"] == BUCKET_KNOWN
    )
    pending_sources = [
        pool_type for pool_type in required_sources if pool_type not in known_sources
    ]
    known_membership = [
        pool_type for pool_type in observed_member_types if pool_type in known_sources
    ]
    scope_complete = not pending_sources

    if len(known_membership) > 1:
        status = POOL_STATUS_CONFLICT
    elif len(known_membership) == 1:
        status = {
            "up": POOL_STATUS_UP,
            "broken": POOL_STATUS_BROKEN,
            "down": POOL_STATUS_DOWN,
        }[known_membership[0]]
    elif scope_complete:
        status = POOL_STATUS_NOT_IN_POOL
    else:
        status = POOL_STATUS_UNKNOWN

    # 已知行的时刻只取已证明来源；没有已证明来源时不产出已知行
    known_moments = [pits[pool_type].get("known_at") for pool_type in known_sources]
    known_at = max(
        (moment for moment in known_moments if moment is not None), default=None
    )
    known_by = KNOWN_CLOSE
    reason_code = None

    unknown = None
    if pending_sources:
        first = pending_sources[0]
        unknown_known_by = pits[first]["known_by"]
        unknown_known_at = pits[first].get("known_at")
        timing_related = any(
            pits[pool_type]["known_by"] in (KNOWN_POST_CLOSE, KNOWN_LATER)
            for pool_type in pending_sources
        )
        suffix = "POST_CUTOFF" if timing_related else "NOT_PROVABLE"
        if len(observed_member_types) > 1:
            unknown_status = POOL_STATUS_CONFLICT
            unknown_reason = f"POOL_CONFLICT_{suffix}"
        elif len(observed_member_types) == 1:
            unknown_status = {
                "up": POOL_STATUS_UP,
                "broken": POOL_STATUS_BROKEN,
                "down": POOL_STATUS_DOWN,
            }[observed_member_types[0]]
            unknown_reason = f"POOL_MEMBERSHIP_{suffix}"
        else:
            unknown_status = POOL_STATUS_UNKNOWN
            unknown_reason = "POOL_MEMBERSHIP_NOT_PROVABLE"
        unknown = {
            "status": unknown_status,
            # 原始观察（含已证明成员）用于审计；UI 展示用下面的 unconfirmed_membership
            "observed_pool_membership": sorted(observed_member_types),
            "unconfirmed_membership": [
                pool_type
                for pool_type in observed_member_types
                if pool_type not in known_sources
            ],
            "pending_sources": pending_sources,
            "reason_code": unknown_reason,
            "known_by": unknown_known_by,
            "known_at": unknown_known_at,
        }

    return {
        "status": status,
        "known_membership": known_membership,
        "scope_complete": scope_complete,
        "pending_sources": pending_sources,
        "known_sources": known_sources,
        "known_by": known_by,
        "known_at": known_at,
        "reason_code": reason_code,
        "unknown": unknown,
        "board_height": (
            membership["up"].consecutive
            if status == POOL_STATUS_UP and "up" in known_membership
            else None
        ),
        "quality": {pool_type: quality[pool_type] for pool_type in POOL_TYPES},
    }


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
            known_at = item.get("known_at")
            if known_at is not None:
                if known_at.tzinfo is None:
                    raise RuntimeError("known_at 必须带时区（Asia/Shanghai）")
                cutoff_raw = item.get("cutoff_at")
                if cutoff_raw:
                    cutoff_moment = datetime.fromisoformat(str(cutoff_raw))
                    if cutoff_moment.tzinfo is None:
                        raise RuntimeError("cutoff_at 必须带时区")
                    if bucket == BUCKET_KNOWN and known_at > cutoff_moment:
                        raise RuntimeError("已知桶出现晚于 cutoff 的 known_at")
                    if (
                        bucket == BUCKET_UNKNOWN
                        and item.get("known_by") in (KNOWN_POST_CLOSE, KNOWN_LATER)
                        and known_at <= cutoff_moment
                    ):
                        raise RuntimeError("后得桶出现不晚于 cutoff 的 known_at")
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
    naive_timezone_trusted: bool | None = None,
) -> dict[str, Any]:
    """构建一只股票在观察日的「已知 / 不知道或后得 / 后验」三段式依据。

    `naive_timezone_trusted` 显式控制「历史 naive 时间戳是否按 +08:00 解释」：
    不传时读 `settings.research_naive_timestamps_trusted`（默认 false），
    绝不在服务层默认信任；无法证明时降级 `UNKNOWN`。
    """
    if not CODE_PATTERN.match(code or ""):
        raise ValueError("股票代码必须是 6 位数字")
    cutoff = (cutoff or CUTOFF_CLOSE).upper()
    if cutoff not in SUPPORTED_CUTOFFS:
        raise ValueError(f"暂不支持的 cutoff：{cutoff}（当前只支持 CLOSE）")
    naive_trusted = resolve_naive_timezone_trusted(naive_timezone_trusted)
    naive_policy = (
        NAIVE_POLICY_CST_ASSUMED if naive_trusted else NAIVE_POLICY_UNTRUSTED
    )
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

    # ---------------- 池状态：先质量、后时间；已知区不放盘后成员/冲突
    pool_logs = {"up": up_log, "broken": broken_log, "down": down_log}
    pool_quality_map = {
        "up": up_quality,
        "broken": broken_quality,
        "down": down_quality,
    }
    pool_trusted = {
        "up": up_trusted,
        "broken": broken_trusted,
        "down": down_trusted,
    }
    pool_pits = {
        pool_type: evaluate_known_time(
            observed_at=(
                pool_logs[pool_type].created_at
                if pool_logs[pool_type] is not None
                else None
            ),
            cutoff_at=cutoff_at,
            data_ready=pool_trusted[pool_type],
            naive_timezone_trusted=naive_trusted,
        )
        for pool_type in POOL_TYPES
    }
    pool_state = _resolve_pool_evidence(
        membership=membership,
        quality=pool_quality_map,
        trusted=pool_trusted,
        pits=pool_pits,
    )
    limit_status = pool_state["status"]
    board_height = pool_state["board_height"]
    pool_needed = [pool_type for pool_type in POOL_TYPES if pool_type in membership] or list(
        POOL_TYPES
    )

    themes = [
        {
            "name": sector_name or sector_code,
            "sector_code": sector_code,
            "taxonomy": taxonomy,
            "fetched_at": fetched_at,
            "known_by": _theme_known_by(
                fetched_at, resolved, naive_timezone_trusted=naive_trusted
            ),
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
        # 只含「来源可信 + 截止前已知」的成员池，不再是数据库当前行的直接投影
        "observed_pool_membership": pool_state["known_membership"],
        "pool_scope_complete": pool_state["scope_complete"],
        "unconfirmed_pool_sources": pool_state["pending_sources"],
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
    if pool_state["unknown"] is not None:
        unknown_reasons.append(pool_state["unknown"]["reason_code"])
    unknown_or_later = {
        "themes": unknown_themes,
        "concepts": concepts,
        "reasons": unknown_reasons,
        # 盘后/无法证明的池成员与冲突只出现在这里，不进已知区
        "pool": pool_state["unknown"],
    }

    # ---------------- D+1 后验（只读，不反写）
    data_as_of = session.scalar(select(func_max_stock_daily()))
    outcome_pct = None
    outcome_close = None
    again = None
    outcome_missing: list[str] = []
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
            outcome_missing = ["pct_chg", "close"]
        else:
            outcome_pct = outcome_daily.pct_chg
            outcome_close = outcome_daily.close
            # 行存在 ≠ 字段有效：NULL 不得映射 0，也不得因此 VERIFIED（P2-05）
            outcome_missing = [
                name
                for name, value in (("pct_chg", outcome_pct), ("close", outcome_close))
                if value is None
            ]
            again = None
            if outcome_missing:
                outcome_status = STATUS_PARTIAL
                outcome_reason = "OUTCOME_FIELDS_NULL"
            elif next_up_trusted:
                outcome_status = STATUS_VERIFIED
                outcome_reason = None
                again = session.get(LimitPool, (following, code, "up")) is not None
            else:
                outcome_status = STATUS_PARTIAL
                outcome_reason = "OUTCOME_LIMIT_POOL_UNVERIFIED"
    later_outcome = {
        "outcome_date": following,
        "status": outcome_status,
        "pct_chg": outcome_pct if following is not None else None,
        "close": outcome_close if following is not None else None,
        "limit_up_again": again if following is not None else None,
        "sample_valid": following is not None and not outcome_missing,
        "missing_fields": outcome_missing if following is not None else [],
        "reason_code": outcome_reason,
        "known_by": KNOWN_FORWARD,
    }

    # ---------------- 质量
    reasons: list[str] = []
    if up_quality != STATUS_VERIFIED:
        reasons.append(up_reason or "POOL_UP_UNVERIFIED")
    if "up" in pool_needed and not (
        up_trusted and pool_pits["up"]["time_bucket"] == BUCKET_KNOWN
    ):
        reasons.append("POOL_NOT_KNOWN_AT_CUTOFF")
    if limit_status == POOL_STATUS_CONFLICT:
        reasons.append("POOL_CONFLICT")
    elif (
        pool_state["pending_sources"]
        and limit_status in (POOL_STATUS_UP, POOL_STATUS_BROKEN, POOL_STATUS_DOWN)
    ):
        # 有已证明的正向观察，但其他池来源未知 → 不能断言「不在另一个池」
        reasons.append("POOL_OTHER_SOURCES_UNKNOWN")
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

    # 池证据拆成两条：截止前可证明的部分进已知桶，未证明的成员/冲突只能进未知桶
    if pool_state["known_membership"] or pool_state["scope_complete"]:
        known_labels = "、".join(pool_state["known_membership"])
        if limit_status == POOL_STATUS_CONFLICT:
            pool_reason = "POOL_CONFLICT"
            pool_summary = (
                f"同一股票同时出现在 {known_labels} 池（均在截止前可证明），不能按优先级择一"
            )
        elif pool_state["known_membership"]:
            pool_reason = "POOL_OTHER_SOURCES_UNKNOWN" if pool_state["pending_sources"] else None
            pool_summary = f"截止前可证明观察到 {known_labels}；判定 {limit_status}"
        else:
            pool_reason = None
            pool_summary = "三个池来源都在截止前可证明：该股不在涨停/炸板/跌停池"
        add(
            "观察日池记录",
            "limit_pool",
            resolved,
            pool_state["known_at"],
            KNOWN_CLOSE,
            up_quality,
            pool_reason,
            pool_summary,
            observed_at=pool_state["known_at"],
            known_at=pool_state["known_at"],
        )

    if pool_state["unknown"] is not None:
        unknown_pool = pool_state["unknown"]
        unknown_labels = "、".join(unknown_pool["observed_pool_membership"])
        if unknown_pool["status"] == POOL_STATUS_CONFLICT:
            unknown_summary = (
                f"同一股票出现在 {unknown_labels} 池，但该冲突未能在截止前全部证明"
            )
        elif unknown_labels:
            unknown_summary = (
                f"观察到 {unknown_labels}，但该成员关系未能在 15:00 前证明"
            )
        else:
            unknown_summary = "三个池来源未能全部证明，无法断言该股不在池"
        add(
            "观察日池记录（截止前未证明）",
            "limit_pool",
            resolved,
            unknown_pool["known_at"],
            unknown_pool["known_by"],
            up_quality,
            unknown_pool["reason_code"],
            unknown_summary,
            observed_at=unknown_pool["known_at"],
            known_at=unknown_pool["known_at"],
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
        # 时点策略显式化：审计时能直接看出 naive 历史时间是否被信任
        "time_policy": {
            "cutoff": cutoff,
            "cutoff_at": cutoff_at_iso,
            "timezone": "Asia/Shanghai",
            "naive_timestamp_policy": naive_policy,
            "naive_timezone_trusted": naive_trusted,
            "write_path_time_source": WRITE_PATH_TIME_SOURCE,
            "note": NAIVE_POLICY_NOTES[naive_policy],
        },
        # 来源质量是「当前审查元数据」，不是 15:00 时点事实（P1-01 第 4 点）
        "source_quality": {
            "note": "当前来源采集质量（审查元数据），不等于观察日 15:00 已知事实",
            "pools": pool_state["quality"],
            "pool_trusted": pool_trusted,
            "pool_known_at": {
                pool_type: (
                    pool_pits[pool_type]["known_at"].isoformat()
                    if pool_pits[pool_type]["known_at"] is not None
                    else None
                )
                for pool_type in POOL_TYPES
            },
        },
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
