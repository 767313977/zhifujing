"""M6.6 逐数据集授权 ledger 一致性测试。"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "reports/trend_engine/m66/provider_approval_ledger.csv"

REQUIRED_COLUMNS = [
    "provider",
    "dataset_or_endpoint",
    "target_gate",
    "account_status",
    "api_permission_status",
    "written_license_status",
    "local_storage_allowed",
    "derived_metrics_allowed",
    "website_internal_use_allowed",
    "public_display_allowed",
    "redistribution_allowed",
    "retention_period",
    "quota_and_rate_limit",
    "pricing",
    "sample_scope",
    "approved_by",
    "approval_date",
    "evidence_uri",
    "approval_status",
]

ALLOWED_STATUS = {
    "NOT_AUTHORIZED",
    "PENDING_VENDOR_REPLY",
    "PENDING_USER_APPROVAL",
    "AUTHORIZED_LIMITED",
    "AUTHORIZED_FULL",
    "REJECTED",
    "EXPIRED",
}

EXPECTED_PROVIDERS = {"iFinD", "Tushare Pro", "理杏仁", "CnOpenData", "Choice", "JoinQuant"}


def _rows() -> list[dict]:
    with LEDGER.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_ledger_columns_complete() -> None:
    rows = _rows()
    assert rows
    assert list(rows[0].keys()) == REQUIRED_COLUMNS
    assert all(None not in row for row in rows)


def test_status_enum_only() -> None:
    assert {row["approval_status"] for row in _rows()} <= ALLOWED_STATUS


def test_every_provider_covered() -> None:
    assert {row["provider"] for row in _rows()} == EXPECTED_PROVIDERS


def test_no_third_party_authorized() -> None:
    for row in _rows():
        if row["provider"] == "iFinD":
            continue
        assert row["approval_status"] in {"NOT_AUTHORIZED", "PENDING_VENDOR_REPLY", "PENDING_USER_APPROVAL", "REJECTED", "EXPIRED"}, row


def test_authorized_rows_require_evidence() -> None:
    for row in _rows():
        if row["approval_status"].startswith("AUTHORIZED"):
            assert row["evidence_uri"], row["dataset_or_endpoint"]


def test_full_authorization_requires_approver_and_date() -> None:
    for row in _rows():
        if row["approval_status"] == "AUTHORIZED_FULL":
            assert row["approved_by"], row
            assert row["approval_date"], row


def test_public_display_not_granted_in_this_stage() -> None:
    for row in _rows():
        assert row["public_display_allowed"] != "ALLOWED", row["provider"]


def test_website_use_only_within_existing_ifind_scope() -> None:
    for row in _rows():
        if row["website_internal_use_allowed"] == "ALLOWED":
            assert row["provider"] == "iFinD", row


@pytest.mark.parametrize("provider", ["Tushare Pro", "理杏仁", "CnOpenData", "Choice", "JoinQuant"])
def test_third_party_storage_not_yet_allowed(provider: str) -> None:
    for row in _rows():
        if row["provider"] == provider:
            assert row["local_storage_allowed"] != "ALLOWED", row["dataset_or_endpoint"]
