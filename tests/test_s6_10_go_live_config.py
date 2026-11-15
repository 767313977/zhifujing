"""S6.10 Go-Live: real auction collection must stay off unless explicitly enabled."""

from __future__ import annotations

from pathlib import Path

from app.config import Settings


def test_auction_collection_defaults_off(monkeypatch):
    monkeypatch.delenv("AUCTION_COLLECT_ENABLED", raising=False)
    assert Settings().auction_collect_enabled is False


def test_auction_collection_can_be_enabled_explicitly(monkeypatch):
    monkeypatch.setenv("AUCTION_COLLECT_ENABLED", "true")
    assert Settings().auction_collect_enabled is True


def test_install_script_pins_auction_off():
    root = Path(__file__).resolve().parents[1]
    install = (root / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert "Environment=AUCTION_COLLECT_ENABLED=false" in install
