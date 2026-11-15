"""M6.6 G9 语义与冲突测试（SYNTHETIC fixture，不联网）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT / "scripts", ROOT / "backend"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import m66_g9_validator as g9  # noqa: E402
from app.services.trend_engine import multisource as ms  # noqa: E402


def test_factor_unknown_cannot_build_adjusted() -> None:
    verdict = g9.validate_factor(8.8814, "UNKNOWN", None)
    assert verdict["reconstructable"] is False
    assert verdict["quality_status"] == "UNKNOWN"
    with pytest.raises(g9.AdjustedConstructionRejected):
        g9.reconstruct_adjusted(1204.98, 8.8814, "UNKNOWN")


def test_factor_without_base_date_is_partial_but_reconstructable() -> None:
    verdict = g9.validate_factor(8.8814, "CUMULATIVE_BACKWARD", None)
    assert verdict["reconstructable"] is True
    assert verdict["quality_status"] == "PARTIAL"
    assert "FACTOR_BASE_DATE_MISSING" in verdict["flags"]


def test_missing_factor_is_not_zero() -> None:
    verdict = g9.validate_factor(None, "UNKNOWN", None)
    assert verdict["reconstructable"] is False
    assert verdict["flags"] == ["FIELD_NOT_PROVIDED"]


def test_announcement_date_never_becomes_timestamp() -> None:
    assert ms.normalize_announcement("20260810") == ("2026-08-10", "date")
    assert ms.normalize_announcement("2026-08-10T00:00:00+08:00") == ("2026-08-10", "date")


def test_multi_row_event_not_double_counted() -> None:
    rows = [
        {"code": "300750", "record_date": "2026-04-21", "ex_date": "2026-04-22", "cash_per_share_pretax": 4.779},
        {"code": "300750", "record_date": "2026-04-21", "ex_date": "2026-04-22", "cash_per_share_pretax": 2.178},
    ]
    aggregated = g9.aggregate_events(rows)
    assert len(aggregated) == 1
    assert aggregated[0]["quality_status"] == "CONFLICT"
    assert aggregated[0]["cash_per_share_pretax"] is None
    assert "CONFLICT_MULTI_PLAN" in aggregated[0]["flags"]


def test_single_plan_multi_row_sums() -> None:
    rows = [
        {"code": "600519", "record_date": "2026-06-25", "ex_date": "2026-06-26", "plan_id": "P1", "cash_per_share_pretax": 10.0},
        {"code": "600519", "record_date": "2026-06-25", "ex_date": "2026-06-26", "plan_id": "P1", "cash_per_share_pretax": 5.0},
    ]
    aggregated = g9.aggregate_events(rows)
    assert aggregated[0]["quality_status"] == "VERIFIED"
    assert aggregated[0]["cash_per_share_pretax"] == 15.0


def test_current_snapshot_cannot_backfill_history() -> None:
    with pytest.raises(ms.StagingViolation):
        ms.assert_snapshot_not_from_current("2025-03-05", "2026-11-16")


def test_missing_fields_stay_null() -> None:
    event = g9.normalize_event({"code": "002594", "record_date": "2026-07-30", "ex_date": "2026-07-31"})
    assert event["cash_per_share_pretax"] is None
    assert event["transfer_ratio"] is None
    assert "FIELD_NOT_PROVIDED:cash_per_share_pretax" in event["flags"]


def test_provider_compare_reports_bp_without_threshold_verdict() -> None:
    left = [{"code": "600519", "trade_date": "2026-06-26", "close": 1168.63}]
    right = [{"code": "600519", "trade_date": "2026-06-26", "close": 1168.60}]
    diffs = g9.compare_providers(left, right, ("code", "trade_date"), "close")
    assert diffs[0]["status"] == "COMPARED"
    assert diffs[0]["rel_bp"] > 0
    assert "pass" not in diffs[0]
