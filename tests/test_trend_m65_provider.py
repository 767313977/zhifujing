"""M6.5 多供应商协议与授权门禁测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.trend_engine import ifind_guard as guard  # noqa: E402
from app.services.trend_engine import multisource as ms  # noqa: E402


def test_unauthorized_providers_hard_rejected() -> None:
    registry = ms.ProviderRegistry(allowed={"iFinD"})
    for provider in ("Tushare", "Lixinger", "CnOpenData", "Choice", "JoinQuant"):
        with pytest.raises(ms.ProviderNotAuthorized):
            registry.get(provider)


def test_unknown_provider_rejected() -> None:
    registry = ms.ProviderRegistry(allowed={"iFinD"})
    with pytest.raises(ms.ProviderNotAuthorized):
        registry.get("Wind")


def test_authorized_but_unregistered_provider_rejected() -> None:
    registry = ms.ProviderRegistry(allowed={"iFinD"})
    with pytest.raises(ms.ProviderNotAuthorized):
        registry.get("iFinD")


def test_not_implemented_provider_never_returns_success() -> None:
    adapter = ms.NotImplementedProvider("Tushare")
    caps = adapter.capabilities()
    assert caps.datasets == ()
    assert caps.license_scope == "NOT_AUTHORIZED"
    with pytest.raises(ms.ProviderNotImplemented):
        adapter.get_corporate_actions(ms.CorporateActionRequest("Tushare", ("600519",), "2026-01-01", "2026-12-31"))
    with pytest.raises(ms.ProviderNotImplemented):
        adapter.get_adjustment_factors(ms.FactorRequest("Tushare", ("600519",), "2026-01-01", "2026-12-31"))
    with pytest.raises(ms.ProviderNotImplemented):
        adapter.get_sector_snapshot(ms.SectorSnapshotRequest("Tushare", ("BK1184.DC",), "2026-01-05"))
    with pytest.raises(ms.ProviderNotImplemented):
        adapter.get_sector_membership_events(ms.MembershipEventRequest("Tushare", ("BK1184.DC",), "2026-01-01", "2026-12-31"))


def test_empty_result_requires_explicit_confirmation() -> None:
    with pytest.raises(ms.ProviderError):
        ms.ProviderResult(
            provider="Tushare",
            dataset="dividend",
            endpoint="dividend",
            records=(),
            raw_payload={"data": []},
        )
    result = ms.ProviderResult(
        provider="Tushare",
        dataset="dividend",
        endpoint="dividend",
        records=(),
        raw_payload={"data": []},
        empty_confirmed=True,
    )
    assert result.records == ()
    assert result.payload_hash


def test_provider_error_code_is_not_empty_data() -> None:
    with pytest.raises(guard.ProviderError):
        guard.ensure_no_provider_error({"code": 40001, "msg": "permission denied"})
    with pytest.raises(guard.ProviderError):
        guard.ensure_no_provider_error({"error": "rate limited"})


def test_payload_hash_is_stable_and_order_independent() -> None:
    left = ms.payload_hash({"b": 2, "a": 1})
    right = ms.payload_hash({"a": 1, "b": 2})
    assert left == right
    assert left != ms.payload_hash({"a": 1, "b": 3})


def test_capabilities_validation() -> None:
    caps = ms.ProviderCapabilities(provider="iFinD", datasets=("index_data",), pit_precision="date")
    assert caps.to_row("batch_x")["provider"] == "iFinD"
    with pytest.raises(ValueError):
        ms.ProviderCapabilities(provider="Wind")
    with pytest.raises(ValueError):
        ms.ProviderCapabilities(provider="Tushare", pit_precision="minute")


def test_registry_rejects_unknown_allowed_provider() -> None:
    with pytest.raises(ValueError):
        ms.ProviderRegistry(allowed={"Wind"})
