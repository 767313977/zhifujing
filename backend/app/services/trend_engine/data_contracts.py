"""M6.7 数据契约：价格域、公司行为、板块成分、PIT 与血缘。

设计原则（与 M6.5/M6.6 护栏一致）：

- 纯函数、无网络、无数据库副作用；
- 缺字段返回 `UNKNOWN`/`MISSING`，**不得**把 NULL 当 0；
- 复权方向/基准日/累计规则不明时禁止构造正式 adjusted 序列；
- 公告日期不得升级为时间戳，生效时间不得冒充公告时间；
- 当前快照不得伪装历史截面；
- `quality_status` 不能仅凭“字段存在”判 PASS。

所有函数返回结构化判定（dict / dataclass），不抛业务异常，便于上游汇总。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MissingValue(str, Enum):
    """空值语义：必须区分六种情况，禁止一律当 0。"""

    PROVIDED = "PROVIDED"
    NOT_PROVIDED = "NOT_PROVIDED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    EXPLICIT_ZERO = "EXPLICIT_ZERO"
    DATA_MISSING = "DATA_MISSING"
    SOURCE_UNSUPPORTED = "SOURCE_UNSUPPORTED"
    UNVERIFIED = "UNVERIFIED"


class QualityStatus(str, Enum):
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"
    CONFLICT = "CONFLICT"
    REJECTED = "REJECTED"
    SYNTHETIC = "SYNTHETIC"


class AdjustmentType(str, Enum):
    RAW = "RAW"
    FORWARD = "FORWARD"
    BACKWARD = "BACKWARD"


class FactorDirection(str, Enum):
    FORWARD = "FORWARD"
    BACKWARD = "BACKWARD"
    UNKNOWN = "UNKNOWN"


class EventType(str, Enum):
    CASH_DIVIDEND = "CASH_DIVIDEND"
    BONUS_SHARE = "BONUS_SHARE"
    TRANSFER = "TRANSFER"
    RIGHTS_ISSUE = "RIGHTS_ISSUE"
    SPLIT = "SPLIT"
    MERGE = "MERGE"
    UNRESOLVED = "UNRESOLVED"


class MembershipStatus(str, Enum):
    MEMBER = "MEMBER"
    NOT_MEMBER = "NOT_MEMBER"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PitTimestamps:
    effective_at: str | None = None
    announced_at: str | None = None
    available_at: str | None = None
    ingested_at: str | None = None
    retrieved_at: str | None = None


@dataclass(frozen=True)
class PriceRecord:
    security_id: str
    trade_date: str
    raw_close: float | None = None
    raw_open: float | None = None
    raw_high: float | None = None
    raw_low: float | None = None
    raw_pre_close: float | None = None
    adjusted_close: float | None = None
    adjustment_type: str | None = None
    adjustment_factor: float | None = None
    factor_direction: str | None = None
    factor_base_date: str | None = None
    factor_cumulative_rule: str | None = None
    source: str | None = None
    source_field: str | None = None
    source_version: str | None = None
    available_at: str | None = None
    ingested_at: str | None = None
    quality_status: str = QualityStatus.UNKNOWN.value
    is_estimated: bool = False


@dataclass(frozen=True)
class CorporateActionRecord:
    event_id: str
    security_id: str
    event_type: str
    cash_dividend_per_share_gross: float | None = None
    bonus_share_ratio: float | None = None
    transfer_share_ratio: float | None = None
    rights_issue_ratio: float | None = None
    rights_issue_price: float | None = None
    rights_issue_shares: float | None = None
    announcement_date: str | None = None
    announcement_timestamp: str | None = None
    record_date: str | None = None
    ex_date: str | None = None
    payment_date: str | None = None
    share_listing_date: str | None = None
    revision_status: str | None = None
    cancelled_at: str | None = None
    available_at: str | None = None
    ingested_at: str | None = None
    source: str | None = None
    source_event_id: str | None = None
    missing_reasons: dict[str, str] = field(default_factory=dict)
    quality_status: str = QualityStatus.UNKNOWN.value


@dataclass(frozen=True)
class SectorMembershipSnapshot:
    provider: str
    provider_sector_id: str | None
    sector_name: str | None
    taxonomy: str
    snapshot_date: str
    security_id: str
    membership_status: str = MembershipStatus.MEMBER.value
    in_date: str | None = None
    out_date: str | None = None
    announced_at: str | None = None
    available_at: str | None = None
    ingested_at: str | None = None
    source_version: str | None = None
    is_current_snapshot: bool = True
    date_param_honored: bool = False
    coverage_scope: str | None = None
    quality_status: str = QualityStatus.UNKNOWN.value


@dataclass(frozen=True)
class SectorProviderMapping:
    provider: str
    b1_sector_id: str
    provider_sector_id: str | None
    mapping_method: str
    confidence: float
    review_status: str = "pending"


MISSING_FIELDS_NUMERIC = (
    "cash_dividend_per_share_gross",
    "bonus_share_ratio",
    "transfer_share_ratio",
    "rights_issue_ratio",
    "rights_issue_price",
    "rights_issue_shares",
)


def classify_missing_value(value: Any, *, reason: str | None = None) -> MissingValue:
    """空值分类：`0` 是有效值，`None`/空串才是缺失。"""
    if reason == "NOT_APPLICABLE":
        return MissingValue.NOT_APPLICABLE
    if reason == "SOURCE_UNSUPPORTED":
        return MissingValue.SOURCE_UNSUPPORTED
    if reason == "DATA_MISSING":
        return MissingValue.DATA_MISSING
    if reason == "UNVERIFIED":
        return MissingValue.UNVERIFIED
    if value is None:
        return MissingValue.NOT_PROVIDED
    if isinstance(value, str) and not value.strip():
        return MissingValue.NOT_PROVIDED
    if isinstance(value, (int, float)) and value == 0:
        return MissingValue.EXPLICIT_ZERO
    return MissingValue.PROVIDED


def validate_adjustment_factor_semantics(
    *,
    factor: float | None,
    direction: str | None,
    base_date: str | None,
    cumulative_rule: str | None,
) -> dict:
    flags: list[str] = []
    if factor is None:
        flags.append("FACTOR_MISSING")
    if direction not in {FactorDirection.FORWARD.value, FactorDirection.BACKWARD.value}:
        flags.append("FACTOR_DIRECTION_UNKNOWN")
    if not base_date:
        flags.append("FACTOR_BASE_DATE_MISSING")
    if not cumulative_rule:
        flags.append("FACTOR_CUMULATIVE_RULE_MISSING")
    constructable = factor is not None and not flags
    return {
        "constructable": constructable,
        "flags": flags,
        "quality_status": QualityStatus.VERIFIED.value if constructable else QualityStatus.UNKNOWN.value,
    }


def validate_price_domain_record(record: PriceRecord) -> dict:
    flags: list[str] = []
    if record.adjustment_type not in {item.value for item in AdjustmentType}:
        flags.append("ADJUSTMENT_TYPE_MISSING")
    if record.adjusted_close is not None and record.adjustment_type in (None, ""):
        flags.append("ADJUSTED_WITHOUT_TYPE")
    if record.raw_close is None:
        flags.append("RAW_CLOSE_MISSING")
    if record.adjustment_type == AdjustmentType.RAW.value and record.adjusted_close is not None:
        flags.append("RAW_ROW_CARRIES_ADJUSTED")
    if record.quality_status == QualityStatus.VERIFIED.value and flags:
        flags.append("QUALITY_OVERCLAIMED")
    status = QualityStatus.UNKNOWN.value
    if not flags:
        status = QualityStatus.VERIFIED.value
    elif "RAW_CLOSE_MISSING" in flags and len(flags) == 1:
        status = QualityStatus.REJECTED.value
    else:
        status = QualityStatus.PARTIAL.value
    return {"quality_status": status, "flags": flags, "usable_as_adjusted": record.adjustment_type in {AdjustmentType.FORWARD.value, AdjustmentType.BACKWARD.value} and record.adjusted_close is not None}


def validate_corporate_action_record(record: CorporateActionRecord) -> dict:
    flags: list[str] = []
    field_status: dict[str, str] = {}
    for name in MISSING_FIELDS_NUMERIC:
        value = getattr(record, name)
        field_status[name] = classify_missing_value(value, reason=record.missing_reasons.get(name)).value

    if record.event_type == EventType.UNRESOLVED.value:
        flags.append("EVENT_TYPE_UNRESOLVED")
    if record.event_type == EventType.CASH_DIVIDEND.value and field_status["cash_dividend_per_share_gross"] != MissingValue.PROVIDED.value:
        flags.append("CASH_AMOUNT_MISSING")
    if record.event_type == EventType.RIGHTS_ISSUE.value:
        for name in ("rights_issue_ratio", "rights_issue_price", "rights_issue_shares"):
            if field_status[name] != MissingValue.PROVIDED.value:
                flags.append(f"RIGHTS_FIELD_MISSING:{name}")
    if record.announcement_timestamp and not record.announcement_date:
        flags.append("TIMESTAMP_WITHOUT_DATE")
    if record.announcement_timestamp and "T00:00:00" in record.announcement_timestamp:
        flags.append("DATE_PADDED_AS_TIMESTAMP")
    if record.announcement_date and record.ex_date and record.announcement_date > record.ex_date:
        flags.append("ANNOUNCEMENT_AFTER_EX_DATE")
    if record.cancelled_at and record.event_type != EventType.UNRESOLVED.value:
        flags.append("CANCELLED_EVENT_RETAINED")
    if record.quality_status == QualityStatus.VERIFIED.value and flags:
        flags.append("QUALITY_OVERCLAIMED")

    status = QualityStatus.PARTIAL.value if flags else QualityStatus.VERIFIED.value
    if "EVENT_TYPE_UNRESOLVED" in flags:
        status = QualityStatus.UNKNOWN.value
    return {"quality_status": status, "flags": flags, "field_status": field_status}


def validate_event_temporal_order(
    *, announcement_date: str | None, record_date: str | None, ex_date: str | None, payment_date: str | None
) -> dict:
    order = {"announcement_date": announcement_date, "record_date": record_date, "ex_date": ex_date, "payment_date": payment_date}
    present = {key: value for key, value in order.items() if value}
    issues = []
    if announcement_date and record_date and announcement_date > record_date:
        issues.append("ANNOUNCEMENT_AFTER_RECORD")
    if record_date and ex_date and record_date > ex_date:
        issues.append("RECORD_AFTER_EX")
    if ex_date and payment_date and ex_date > payment_date:
        issues.append("EX_AFTER_PAYMENT")
    return {"ordered_fields": list(present), "issues": issues, "ok": not issues}


def evaluate_pit_eligibility(
    *, pit: PitTimestamps, requires_announcement_time: bool = False
) -> dict:
    flags: list[str] = []
    if not pit.available_at:
        flags.append("AVAILABLE_AT_MISSING")
    if not pit.announced_at:
        flags.append("ANNOUNCED_AT_MISSING")
    if requires_announcement_time and pit.announced_at and len(str(pit.announced_at)) <= 10:
        flags.append("ANNOUNCEMENT_ONLY_DATE")
    if pit.ingested_at and pit.available_at and pit.ingested_at < pit.available_at:
        flags.append("INGESTED_BEFORE_AVAILABLE")
    status = "PIT_UNVERIFIED" if "AVAILABLE_AT_MISSING" in flags else ("DATE_LEVEL_ONLY" if "ANNOUNCEMENT_ONLY_DATE" in flags else "PIT_OK")
    return {"status": status, "flags": flags, "strict_oos_usable": status == "PIT_OK"}


def evaluate_snapshot_historical_eligibility(snapshot: SectorMembershipSnapshot) -> dict:
    flags: list[str] = []
    if snapshot.is_current_snapshot:
        flags.append("CURRENT_SNAPSHOT_ONLY")
    if not snapshot.date_param_honored:
        flags.append("DATE_PARAM_NOT_HONORED")
    if snapshot.available_at and snapshot.available_at[:10] > snapshot.snapshot_date[:10]:
        flags.append("AVAILABLE_AFTER_SNAPSHOT_DATE")
    if snapshot.quality_status == QualityStatus.VERIFIED.value and flags:
        flags.append("QUALITY_OVERCLAIMED")
    eligible = not flags
    return {
        "historical_eligible": eligible,
        "status": "HISTORICAL_OK" if eligible else "CURRENT_SNAPSHOT_ONLY",
        "flags": flags,
    }


def evaluate_data_lineage_completeness(record: dict[str, Any], required: tuple[str, ...]) -> dict:
    missing = [name for name in required if not record.get(name)]
    return {
        "complete": not missing,
        "missing": missing,
        "quality_status": QualityStatus.VERIFIED.value if not missing else QualityStatus.PARTIAL.value,
    }


def classify_data_quality(
    *, flags: list[str], evidence_level: str, is_synthetic: bool = False
) -> str:
    """由证据等级与标志汇总质量状态；不因“字段存在”直接给 VERIFIED。"""
    if is_synthetic:
        return QualityStatus.SYNTHETIC.value
    if "CONFLICT" in flags:
        return QualityStatus.CONFLICT.value
    if any(flag.endswith("_MISSING") or flag in {"ADJUSTMENT_TYPE_MISSING", "FACTOR_DIRECTION_UNKNOWN"} for flag in flags):
        return QualityStatus.UNKNOWN.value
    if flags:
        return QualityStatus.PARTIAL.value
    if evidence_level not in {"REAL_RESPONSE", "VENDOR_DOCUMENT"}:
        return QualityStatus.PARTIAL.value
    return QualityStatus.VERIFIED.value


def mapping_is_approved(mapping: SectorProviderMapping) -> dict:
    approved = mapping.review_status == "approved" and mapping.mapping_method in {
        "official_code_table",
        "manual_reviewed",
        "exact_code_match",
    }
    return {
        "approved": approved,
        "candidate_only": not approved,
        "reason": "" if approved else "FuzzyOrUnreviewedMapping",
    }
