"""M6.7 G3 历史板块成分契约测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT / "backend",):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from app.services.trend_engine import data_contracts as dc  # noqa: E402
from app.services.trend_engine import multisource as ms  # noqa: E402


def _snapshot(**overrides):
    base = dict(
        provider="iFinD",
        provider_sector_id=None,
        sector_name="芯片概念",
        taxonomy="IFIND_CONCEPT",
        snapshot_date="2025-03-05",
        security_id="688981.SH",
        is_current_snapshot=False,
        date_param_honored=True,
        available_at="2025-03-05T18:00:00+08:00",
        quality_status="VERIFIED",
    )
    base.update(overrides)
    return dc.SectorMembershipSnapshot(**base)


def test_current_snapshot_is_not_historical() -> None:
    verdict = dc.evaluate_snapshot_historical_eligibility(
        _snapshot(is_current_snapshot=True, date_param_honored=False, quality_status="PARTIAL")
    )
    assert verdict["historical_eligible"] is False
    assert verdict["status"] == "CURRENT_SNAPSHOT_ONLY"
    assert set(verdict["flags"]) >= {"CURRENT_SNAPSHOT_ONLY", "DATE_PARAM_NOT_HONORED"}


def test_date_param_ignored_cannot_be_accepted() -> None:
    verdict = dc.evaluate_snapshot_historical_eligibility(
        _snapshot(is_current_snapshot=False, date_param_honored=False, quality_status="PARTIAL")
    )
    assert verdict["historical_eligible"] is False
    assert "DATE_PARAM_NOT_HONORED" in verdict["flags"]


def test_available_after_snapshot_date_flagged() -> None:
    verdict = dc.evaluate_snapshot_historical_eligibility(
        _snapshot(available_at="2026-11-16T09:00:00+08:00", quality_status="PARTIAL")
    )
    assert "AVAILABLE_AFTER_SNAPSHOT_DATE" in verdict["flags"]
    assert verdict["historical_eligible"] is False


def test_true_historical_snapshot_is_eligible() -> None:
    verdict = dc.evaluate_snapshot_historical_eligibility(_snapshot())
    assert verdict["historical_eligible"] is True
    assert verdict["status"] == "HISTORICAL_OK"


def test_snapshot_only_must_not_carry_in_out() -> None:
    with pytest.raises(ms.StagingViolation):
        ms.assert_snapshot_mode(ms.SNAPSHOT_ONLY, "2025-03-05", None)
    with pytest.raises(ms.StagingViolation):
        ms.assert_snapshot_mode(ms.SNAPSHOT_ONLY, None, "2025-06-01")
    ms.assert_snapshot_mode(ms.SNAPSHOT_ONLY, None, None)


def test_current_membership_cannot_backfill_history() -> None:
    with pytest.raises(ms.StagingViolation):
        ms.assert_snapshot_not_from_current("2025-03-05", "2026-11-16")


def test_fuzzy_mapping_is_candidate_only() -> None:
    mapping = dc.SectorProviderMapping(
        provider="Tushare",
        b1_sector_id="801001",
        provider_sector_id="BK1036.DC",
        mapping_method="name_fuzzy_candidate",
        confidence=0.4,
    )
    verdict = dc.mapping_is_approved(mapping)
    assert verdict["approved"] is False
    assert verdict["candidate_only"] is True
    assert verdict["reason"] == "FuzzyOrUnreviewedMapping"


def test_label_pending_even_if_method_official() -> None:
    mapping = dc.SectorProviderMapping(
        provider="Tushare",
        b1_sector_id="881120",
        provider_sector_id="801730.SI",
        mapping_method="official_code_table",
        confidence=1.0,
        review_status="pending",
    )
    assert dc.mapping_is_approved(mapping)["approved"] is False


def test_official_mapping_approved() -> None:
    mapping = dc.SectorProviderMapping(
        provider="Tushare",
        b1_sector_id="881120",
        provider_sector_id="801730.SI",
        mapping_method="official_code_table",
        confidence=1.0,
        review_status="approved",
    )
    assert dc.mapping_is_approved(mapping)["approved"] is True


def test_same_name_across_taxonomy_not_auto_equal() -> None:
    left = _snapshot(taxonomy="IFIND_CONCEPT", provider_sector_id=None)
    right = _snapshot(taxonomy="TUSHARE_DC", provider_sector_id="BK1184.DC")
    assert left.taxonomy != right.taxonomy
    assert left.provider_sector_id != right.provider_sector_id
