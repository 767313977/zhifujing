"""M6.4 iFinD 接入护栏测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.trend_engine import ifind_guard as guard  # noqa: E402


def test_budget_hard_stop() -> None:
    budget = guard.RequestBudget(limit_cells=100)
    budget.record(50)
    with pytest.raises(guard.BudgetExceeded):
        budget.record(51)
    assert budget.used_cells == 50


def test_budget_warn_at_80_percent() -> None:
    budget = guard.RequestBudget(limit_cells=100)
    budget.record(80)
    assert any(event.startswith("BUDGET_WARN") for event in budget.events)


def test_rate_limiter_spacing() -> None:
    clock = {"now": 0.0}
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["now"] += seconds

    limiter = guard.RateLimiter(qps=2.0, clock=lambda: clock["now"], sleep=fake_sleep)
    limiter.acquire()
    limiter.acquire()
    assert slept and slept[0] == pytest.approx(0.5)


def test_provider_error_not_empty_data() -> None:
    with pytest.raises(guard.ProviderError):
        guard.ensure_no_provider_error({"error": "quota exceeded"})
    with pytest.raises(guard.ProviderError):
        guard.ensure_no_provider_error({"ok": False})
    guard.ensure_no_provider_error({"data": []})


def test_schema_mismatch_on_empty_or_missing_fields() -> None:
    with pytest.raises(guard.SchemaMismatch):
        guard.require_fields([], ("close",), context="index")
    with pytest.raises(guard.SchemaMismatch):
        guard.require_fields([{"close": 1.0}], ("close", "trade_date"), context="index")
    guard.require_fields([{"close": 1.0, "trade_date": "2026-10-09"}], ("close", "trade_date"))


def test_normalize_code_and_date() -> None:
    assert guard.normalize_code("600519") == "600519"
    assert guard.normalize_code("600519.sh") == "600519.SH"
    with pytest.raises(ValueError):
        guard.normalize_code("60051")
    assert guard.normalize_date("20261009") == "2026-10-09"
    with pytest.raises(ValueError):
        guard.normalize_date("2026/10/09")


def test_payload_hash_stable() -> None:
    left = guard.payload_hash({"b": 2, "a": 1})
    right = guard.payload_hash({"a": 1, "b": 2})
    assert left == right


def test_available_at_not_fabricated() -> None:
    assert guard.available_at_or_null(None) is None
    assert guard.available_at_or_null("") is None
    assert guard.available_at_or_null("2026-10-09 18:00:00") == "2026-10-09 18:00:00"
