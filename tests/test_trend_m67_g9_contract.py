"""M6.7 G9 价格域与公司行为契约测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.trend_engine import data_contracts as dc  # noqa: E402


def test_null_zero_and_not_applicable_are_distinct() -> None:
    assert dc.classify_missing_value(None) is dc.MissingValue.NOT_PROVIDED
    assert dc.classify_missing_value(0) is dc.MissingValue.EXPLICIT_ZERO
    assert dc.classify_missing_value(0.0) is dc.MissingValue.EXPLICIT_ZERO
    assert dc.classify_missing_value(None, reason="NOT_APPLICABLE") is dc.MissingValue.NOT_APPLICABLE
    assert dc.classify_missing_value(None, reason="SOURCE_UNSUPPORTED") is dc.MissingValue.SOURCE_UNSUPPORTED
    assert dc.classify_missing_value("") is dc.MissingValue.NOT_PROVIDED


def test_adjusted_price_without_type_is_rejected() -> None:
    record = dc.PriceRecord(
        security_id="600519.SH",
        trade_date="2026-06-26",
        raw_close=1168.63,
        adjusted_close=10377.11,
        adjustment_type=None,
    )
    verdict = dc.validate_price_domain_record(record)
    assert "ADJUSTED_WITHOUT_TYPE" in verdict["flags"]
    assert verdict["quality_status"] == dc.QualityStatus.PARTIAL.value
    assert verdict["usable_as_adjusted"] is False


def test_raw_row_must_not_carry_adjusted_value() -> None:
    record = dc.PriceRecord(
        security_id="600519.SH",
        trade_date="2026-06-26",
        raw_close=1168.63,
        adjusted_close=10377.11,
        adjustment_type="RAW",
    )
    verdict = dc.validate_price_domain_record(record)
    assert "RAW_ROW_CARRIES_ADJUSTED" in verdict["flags"]


def test_factor_semantics_missing_blocks_adjusted_series() -> None:
    verdict = dc.validate_adjustment_factor_semantics(
        factor=8.8814, direction=None, base_date=None, cumulative_rule=None
    )
    assert verdict["constructable"] is False
    assert verdict["quality_status"] == dc.QualityStatus.UNKNOWN.value
    assert set(verdict["flags"]) == {
        "FACTOR_DIRECTION_UNKNOWN",
        "FACTOR_BASE_DATE_MISSING",
        "FACTOR_CUMULATIVE_RULE_MISSING",
    }

    complete = dc.validate_adjustment_factor_semantics(
        factor=8.8814, direction="BACKWARD", base_date="2026-06-26", cumulative_rule="PRODUCT"
    )
    assert complete["constructable"] is True


def test_cash_dividend_missing_amount_blocks_verified() -> None:
    record = dc.CorporateActionRecord(
        event_id="600519-2026-06-26",
        security_id="600519.SH",
        event_type="CASH_DIVIDEND",
        record_date="2026-06-25",
        ex_date="2026-06-26",
    )
    verdict = dc.validate_corporate_action_record(record)
    assert "CASH_AMOUNT_MISSING" in verdict["flags"]
    assert verdict["quality_status"] == dc.QualityStatus.PARTIAL.value
    assert verdict["field_status"]["cash_dividend_per_share_gross"] == "NOT_PROVIDED"


def test_rights_issue_fields_are_independent() -> None:
    record = dc.CorporateActionRecord(
        event_id="300176-2026-08",
        security_id="300176.SZ",
        event_type="RIGHTS_ISSUE",
        rights_issue_ratio=0.4,
        rights_issue_price=3.36,
        rights_issue_shares=144_170_000.0,
        announcement_date="2026-08-10",
        ex_date="2026-08-21",
        quality_status="VERIFIED",
    )
    verdict = dc.validate_corporate_action_record(record)
    assert verdict["flags"] == []
    assert verdict["field_status"]["rights_issue_ratio"] == "PROVIDED"


def test_rights_issue_missing_shares_not_derived() -> None:
    record = dc.CorporateActionRecord(
        event_id="x",
        security_id="300176.SZ",
        event_type="RIGHTS_ISSUE",
        rights_issue_ratio=0.4,
        rights_issue_price=3.36,
    )
    verdict = dc.validate_corporate_action_record(record)
    assert "RIGHTS_FIELD_MISSING:rights_issue_shares" in verdict["flags"]


def test_unresolved_event_never_auto_classified() -> None:
    record = dc.CorporateActionRecord(
        event_id="300750-2026-04-22",
        security_id="300750.SZ",
        event_type="UNRESOLVED",
        ex_date="2026-04-22",
        quality_status="VERIFIED",
    )
    verdict = dc.validate_corporate_action_record(record)
    assert verdict["quality_status"] == dc.QualityStatus.UNKNOWN.value
    assert "EVENT_TYPE_UNRESOLVED" in verdict["flags"]


def test_date_padded_as_timestamp_is_flagged() -> None:
    record = dc.CorporateActionRecord(
        event_id="x",
        security_id="600519.SH",
        event_type="CASH_DIVIDEND",
        announcement_date="2026-06-26",
        announcement_timestamp="2026-06-26T00:00:00+08:00",
        cash_dividend_per_share_gross=23.0,
    )
    verdict = dc.validate_corporate_action_record(record)
    assert "DATE_PADDED_AS_TIMESTAMP" in verdict["flags"]


def test_cancelled_event_is_retained_with_flag() -> None:
    record = dc.CorporateActionRecord(
        event_id="x",
        security_id="600519.SH",
        event_type="CASH_DIVIDEND",
        cash_dividend_per_share_gross=23.0,
        cancelled_at="2026-06-20",
    )
    verdict = dc.validate_corporate_action_record(record)
    assert "CANCELLED_EVENT_RETAINED" in verdict["flags"]


def test_temporal_order_validation() -> None:
    ok = dc.validate_event_temporal_order(
        announcement_date="2026-06-15", record_date="2026-06-25", ex_date="2026-06-26", payment_date="2026-06-26"
    )
    assert ok["ok"] is True
    bad = dc.validate_event_temporal_order(
        announcement_date="2026-06-27", record_date="2026-06-25", ex_date="2026-06-26", payment_date="2026-06-20"
    )
    assert bad["ok"] is False
    assert set(bad["issues"]) == {"ANNOUNCEMENT_AFTER_RECORD", "EX_AFTER_PAYMENT"}


def test_pit_missing_available_at_is_unverified() -> None:
    verdict = dc.evaluate_pit_eligibility(pit=dc.PitTimestamps(announced_at="2026-06-26"))
    assert verdict["status"] == "PIT_UNVERIFIED"
    assert verdict["strict_oos_usable"] is False


def test_date_only_announcement_is_date_level() -> None:
    verdict = dc.evaluate_pit_eligibility(
        pit=dc.PitTimestamps(announced_at="2026-06-26", available_at="2026-06-26T09:00:00+08:00"),
        requires_announcement_time=True,
    )
    assert verdict["status"] == "DATE_LEVEL_ONLY"
    assert verdict["strict_oos_usable"] is False


def test_ingested_before_available_flagged() -> None:
    verdict = dc.evaluate_pit_eligibility(
        pit=dc.PitTimestamps(
            announced_at="2026-06-26T10:00:00+08:00",
            available_at="2026-06-27T09:00:00+08:00",
            ingested_at="2026-06-26T18:00:00+08:00",
        )
    )
    assert "INGESTED_BEFORE_AVAILABLE" in verdict["flags"]


def test_quality_not_verified_from_field_presence_alone() -> None:
    assert dc.classify_data_quality(flags=[], evidence_level="LOCAL_TEST") == "PARTIAL"
    assert dc.classify_data_quality(flags=[], evidence_level="REAL_RESPONSE") == "VERIFIED"
    assert dc.classify_data_quality(flags=["FACTOR_MISSING"], evidence_level="REAL_RESPONSE") == "UNKNOWN"
    assert dc.classify_data_quality(flags=["CONFLICT"], evidence_level="REAL_RESPONSE") == "CONFLICT"
    assert dc.classify_data_quality(flags=[], evidence_level="REAL_RESPONSE", is_synthetic=True) == "SYNTHETIC"


def test_contract_is_deterministic() -> None:
    record = dc.CorporateActionRecord(
        event_id="x", security_id="300176.SZ", event_type="RIGHTS_ISSUE", rights_issue_ratio=0.4
    )
    left = json.dumps(dc.validate_corporate_action_record(record), sort_keys=True, ensure_ascii=False)
    right = json.dumps(dc.validate_corporate_action_record(record), sort_keys=True, ensure_ascii=False)
    assert left == right


def test_lineage_completeness() -> None:
    verdict = dc.evaluate_data_lineage_completeness(
        {"provider": "iFinD", "dataset": "get_stock_events", "source_field": "股权登记日"},
        required=("provider", "dataset", "source_field", "transform_version"),
    )
    assert verdict["complete"] is False
    assert verdict["missing"] == ["transform_version"]
    assert verdict["quality_status"] == "PARTIAL"
