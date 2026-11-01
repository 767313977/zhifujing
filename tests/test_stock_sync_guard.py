"""`POST /api/stock/{code}/sync` 的存在性校验（2026-11-01 审计 P2-3）。

`_code()` 只保证 6 位数字，假代码（999999）也能过 —— 不挡就会按缺块发 iFinD 请求，
把共享配额烧掉。这里钉住「查不到就 400」，且**不会**走到采集器。
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api import stock as stock_api
from app.models import Base, StockBasic


def test_sync_rejects_unknown_code():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        with pytest.raises(HTTPException) as exc:
            stock_api.sync("999999", 250, session=session, user=SimpleNamespace(id=1))
    assert exc.value.status_code == 400
    assert "查不到这个代码" in str(exc.value.detail)


def test_sync_known_code_passes_existence_check(monkeypatch):
    """已知代码能过存在性关卡：把采集器换成假的，确认它确实被调用。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    called: list[tuple[str, int]] = []

    class _FakeCollector:
        def sync_stock(self, code: str, *, days: int) -> int:
            called.append((code, days))
            return 3

    monkeypatch.setattr(stock_api, "DailyCollector", _FakeCollector)
    monkeypatch.setattr(stock_api, "_guard_quota", lambda: None)
    monkeypatch.setattr(
        stock_api,
        "_sync_cooldown",
        SimpleNamespace(touch=lambda key: None, remaining=lambda key: None),
    )

    from contextlib import contextmanager

    @contextmanager
    def _guard(reason: str):
        yield

    monkeypatch.setattr(stock_api, "collect_guard", _guard)

    with Session(engine) as session:
        session.add(StockBasic(code="600000", name="浦发银行"))
        session.commit()
        result = stock_api.sync("600000", 120, session=session, user=SimpleNamespace(id=1))

    assert result == {"rows": 3}
    assert called == [("600000", 120)]
