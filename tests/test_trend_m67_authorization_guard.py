"""M6.7 授权守卫与 Fake Provider 测试（fail closed）。"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.trend_engine import authz as az  # noqa: E402


def _record(**overrides) -> az.AuthorizationRecord:
    base = dict(
        provider="Tushare",
        dataset="adj_factor",
        approval_status="AUTHORIZED_LIMITED",
        evidence_uri="https://tushare.pro/document/2?doc_id=28",
        approved_by="user",
        approval_date="2026-11-16",
        expires_at="2027-11-16",
        local_storage_allowed="ALLOWED",
        derived_metrics_allowed="ALLOWED",
        website_internal_use_allowed="NOT_ALLOWED",
        public_display_allowed="NOT_ALLOWED",
        redistribution_allowed="NOT_ALLOWED",
    )
    base.update(overrides)
    return az.AuthorizationRecord(**base)


def test_no_ledger_row_denied() -> None:
    guard = az.AuthorizationGuard([])
    decision = guard.check(provider="Tushare", dataset="adj_factor", purpose="PERSONAL_RESEARCH")
    assert decision == {"allowed": False, "reason": "NO_LEDGER_ROW"}


@pytest.mark.parametrize("status", ["NOT_AUTHORIZED", "REJECTED", "UNKNOWN", "EXPIRED", "PENDING_USER_APPROVAL"])
def test_non_authorized_status_denied(status: str) -> None:
    guard = az.AuthorizationGuard([_record(approval_status=status)])
    decision = guard.check(provider="Tushare", dataset="adj_factor", purpose="PERSONAL_RESEARCH")
    assert decision["allowed"] is False
    assert decision["reason"] == f"STATUS_{status}"


def test_missing_evidence_uri_denied() -> None:
    guard = az.AuthorizationGuard([_record(evidence_uri="")])
    assert guard.check(provider="Tushare", dataset="adj_factor", purpose="PERSONAL_RESEARCH")["reason"] == "EVIDENCE_URI_MISSING"


def test_missing_approver_denied() -> None:
    guard = az.AuthorizationGuard([_record(approved_by="")])
    assert guard.check(provider="Tushare", dataset="adj_factor", purpose="PERSONAL_RESEARCH")["reason"] == "APPROVER_OR_DATE_MISSING"


def test_expired_license_denied() -> None:
    guard = az.AuthorizationGuard([_record(expires_at="2026-11-15")])
    decision = guard.check(
        provider="Tushare", dataset="adj_factor", purpose="PERSONAL_RESEARCH", today=date(2026, 11, 16)
    )
    assert decision["reason"] == "EXPIRED"


def test_personal_research_allowed_but_website_denied() -> None:
    guard = az.AuthorizationGuard([_record()])
    assert guard.check(provider="Tushare", dataset="adj_factor", purpose="PERSONAL_RESEARCH")["allowed"] is True
    assert guard.check(provider="Tushare", dataset="adj_factor", purpose="INTERNAL_WEBSITE")["reason"] == "INTERNAL_WEBSITE_NOT_ALLOWED"
    assert guard.check(provider="Tushare", dataset="adj_factor", purpose="PUBLIC_DISPLAY")["reason"] == "PUBLIC_DISPLAY_NOT_ALLOWED"


def test_raw_storage_requires_explicit_permission() -> None:
    guard = az.AuthorizationGuard([_record(local_storage_allowed="NOT_ALLOWED")])
    decision = guard.check(
        provider="Tushare", dataset="adj_factor", purpose="PERSONAL_RESEARCH", storage="RAW_LOCAL"
    )
    assert decision["reason"] == "RAW_STORAGE_NOT_ALLOWED"
    assert guard.check(
        provider="Tushare", dataset="adj_factor", purpose="PERSONAL_RESEARCH", storage="SUMMARY_ONLY"
    )["allowed"] is True


def test_derived_metrics_requires_permission() -> None:
    guard = az.AuthorizationGuard([_record(derived_metrics_allowed="NOT_ALLOWED")])
    assert guard.check(
        provider="Tushare", dataset="adj_factor", purpose="DERIVED_METRICS"
    )["reason"] == "DERIVED_METRICS_NOT_ALLOWED"


def test_redistribution_requires_permission() -> None:
    guard = az.AuthorizationGuard([_record()])
    assert guard.check(
        provider="Tushare", dataset="adj_factor", purpose="REDISTRIBUTION"
    )["reason"] == "REDISTRIBUTION_NOT_ALLOWED"


def test_require_raises() -> None:
    guard = az.AuthorizationGuard([_record(public_display_allowed="NOT_ALLOWED")])
    with pytest.raises(az.AuthorizationDenied):
        guard.require(provider="Tushare", dataset="adj_factor", purpose="PUBLIC_DISPLAY")


def test_full_authorization_allows_display() -> None:
    record = _record(
        approval_status="AUTHORIZED_FULL",
        website_internal_use_allowed="ALLOWED",
        public_display_allowed="ALLOWED",
        redistribution_allowed="ALLOWED",
    )
    guard = az.AuthorizationGuard([record])
    assert guard.check(provider="Tushare", dataset="adj_factor", purpose="PUBLIC_DISPLAY")["allowed"] is True


def test_invalid_purpose_and_storage_rejected() -> None:
    guard = az.AuthorizationGuard([_record()])
    with pytest.raises(ValueError):
        guard.check(provider="Tushare", dataset="adj_factor", purpose="WHATEVER")
    with pytest.raises(ValueError):
        guard.check(provider="Tushare", dataset="adj_factor", purpose="PERSONAL_RESEARCH", storage="FULL_DUMP")


def test_fake_provider_is_synthetic_and_never_calls_network() -> None:
    fake = az.FakeProvider(
        provider="Tushare",
        dataset="dividend",
        fixture={"records": [{"event": "SYNTHETIC"}]},
    )
    caps = fake.capabilities()
    assert caps["data_origin"] == "SYNTHETIC_FIXTURE"
    assert caps["network_enabled"] is False
    payload = fake.fetch()
    assert payload["data_origin"] == "SYNTHETIC_FIXTURE"
    assert payload["network_calls"] == 0
    assert fake.network_calls == 0


def test_summary_counts() -> None:
    guard = az.AuthorizationGuard([_record()])
    decisions = [
        guard.check(provider="Tushare", dataset="adj_factor", purpose="PERSONAL_RESEARCH"),
        guard.check(provider="Tushare", dataset="adj_factor", purpose="PUBLIC_DISPLAY"),
        guard.check(provider="Choice", dataset="em_sector", purpose="PERSONAL_RESEARCH"),
    ]
    assert az.summarize_decisions(decisions) == {"total": 3, "allowed": 1, "denied": 2}
