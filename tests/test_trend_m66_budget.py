"""M6.6 请求预算守卫与用途闸门测试（对应增补任务书第 10 章）。"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.trend_engine import budget_guard as bg  # noqa: E402


def _guard(tmp_path: Path, allowed: set[str] | None = None) -> bg.RequestBudgetGuard:
    return bg.RequestBudgetGuard(tmp_path / "ledger.jsonl", allowed_tools=allowed)


def test_101st_request_rejected(tmp_path: Path) -> None:
    guard = _guard(tmp_path)
    for index in range(100):
        guard.reserve(tool_name="get_stock_performance", params_hash=f"h{index}")
    assert guard.used == 100
    with pytest.raises(bg.BudgetExhausted):
        guard.reserve(tool_name="get_stock_performance", params_hash="h101")
    assert guard.used == 100
    assert guard.summary()["rejected"] == 1


def test_concurrent_requests_cannot_break_limit(tmp_path: Path) -> None:
    guard = _guard(tmp_path)
    ok: list[int] = []
    failed: list[int] = []
    lock = threading.Lock()

    def worker(base: int) -> None:
        for offset in range(15):
            try:
                guard.reserve(tool_name="index_data", params_hash=f"{base}-{offset}")
                with lock:
                    ok.append(1)
            except bg.BudgetExhausted:
                with lock:
                    failed.append(1)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(ok) == 100
    assert len(failed) == 20
    assert guard.used == 100


def test_timeout_request_still_counted(tmp_path: Path) -> None:
    guard = _guard(tmp_path)
    reservation = guard.reserve(tool_name="get_stock_events", params_hash="h1")
    guard.record(reservation, response_status="TIMEOUT")
    assert guard.used == 1


def test_retry_is_counted_separately(tmp_path: Path) -> None:
    guard = _guard(tmp_path)
    first = guard.reserve(tool_name="get_stock_events", params_hash="h1", purpose="G9")
    guard.record(first, response_status="ERROR", provider_error_code="503")
    retry = guard.reserve(tool_name="get_stock_events", params_hash="h1", purpose="RETRY")
    guard.record(retry, response_status="OK", retry_of=first.request_id)
    assert guard.used == 2
    assert guard.summary()["by_purpose"]["RETRY"] == 1


def test_corrupt_ledger_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(bg.BudgetLedgerCorrupt):
        bg.RequestBudgetGuard(path)

    path.write_text(json.dumps({"type": "header", "limit": 100}) + "\n{bad}\n", encoding="utf-8")
    with pytest.raises(bg.BudgetLedgerCorrupt):
        bg.RequestBudgetGuard(path)


def test_limit_cannot_be_raised(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        bg.RequestBudgetGuard(tmp_path / "ledger.jsonl", limit=150)


def test_tool_not_in_allowlist_rejected(tmp_path: Path) -> None:
    guard = _guard(tmp_path, allowed={"get_stock_performance"})
    with pytest.raises(bg.ToolNotAllowed):
        guard.reserve(tool_name="index_data", params_hash="h")


def test_credentials_never_enter_ledger(tmp_path: Path) -> None:
    guard = _guard(tmp_path)
    with pytest.raises(ValueError):
        guard.reserve(tool_name="x", params_hash="Authorization: Bearer abc123")
    guard.reserve(tool_name="x", params_hash="h1")
    text = (tmp_path / "ledger.jsonl").read_text(encoding="utf-8")
    assert "Bearer" not in text
    assert "abc123" not in text


def test_redact_masks_tokens() -> None:
    masked = bg.redact("Authorization: Bearer abcdef123456 and api_key=xyz")
    assert "abcdef123456" not in masked
    assert "xyz" not in masked


def test_website_publish_blocked_without_license() -> None:
    rows = [{"provider": "iFinD", "public_display_allowed": "NOT_ALLOWED"}]
    with pytest.raises(bg.WebsiteLicenseNotEstablished):
        bg.assert_website_publish_allowed(rows, action="export")
    bg.assert_website_publish_allowed(
        [{"provider": "iFinD", "public_display_allowed": "ALLOWED"}], action="export"
    )


def test_payload_persistence_defaults_to_summary_only() -> None:
    assert bg.payload_persistence_policy(False) == "SUMMARY_ONLY"
    assert bg.payload_persistence_policy(True) == "ALLOW_RAW"


def test_staging_is_gitignored() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "backend/data/" in gitignore


def test_summary_reports_remaining(tmp_path: Path) -> None:
    guard = _guard(tmp_path)
    guard.reserve(tool_name="a", params_hash="h1")
    guard.reserve(tool_name="a", params_hash="h2")
    guard.record(guard.reserve(tool_name="a", params_hash="h3"), response_status="OK")
    summary = guard.summary()
    assert summary["used"] == 3
    assert summary["remaining"] == 97
