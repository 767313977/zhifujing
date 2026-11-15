"""S6.10 regression: stock phase calendar must use the read-only research session.

The S6.10 core-business acceptance run against a split read-only research DB
returned 500 on `/api/stock/000001` because `_trade_day_index()` used
`session_scope()` (write-capable). This test pins the read-only session.
"""

from __future__ import annotations

from contextlib import contextmanager

from app.services import stock_phase


class _EmptyScalars:
    def __iter__(self):
        return iter(())


class _FakeSession:
    def scalars(self, _stmt):
        return _EmptyScalars()


def test_trade_day_index_uses_research_session(monkeypatch):
    calls: list[str] = []

    @contextmanager
    def fake_scope():
        calls.append("research")
        yield _FakeSession()

    stock_phase._TRADE_DAY_INDEX = None
    monkeypatch.setattr(stock_phase, "research_session_scope", fake_scope)
    index = stock_phase._trade_day_index()
    assert index == {}
    assert calls == ["research"]
