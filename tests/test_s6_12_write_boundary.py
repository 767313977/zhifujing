"""S6.12 P0-1：研究数据写入边界负向测试。

验证：
- 普通请求（`http_request_scope` 默认）不能写研究数据；
- 后台（无请求上下文 / 管理员显式放行）仍可写；
- 手动同步接口明确 409，且不写 `stock_daily`；
- 板块成分股 GET 纯读，不落 `sector_member`；
- 用户 A 不能删除用户 B 的自选。
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select

from app.api import sector as sector_api
from app.api import stock as stock_api
from app.api import watchlist as watchlist_api
from app.db import SessionLocal
from app.models import AppUser, SectorBasic, SectorMember, StockBasic, StockDaily, StockEventCache, Watchlist
from app.runtime_context import http_request_scope
from app.services import stock_events, usage
from app.services.usage import IfindUsage


def _count(model) -> int:
    with SessionLocal() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _cleanup() -> None:
    with SessionLocal() as session:
        session.execute(delete(StockDaily).where(StockDaily.code == "S61201"))
        session.execute(delete(StockBasic).where(StockBasic.code == "S61201"))
        session.execute(delete(SectorMember).where(SectorMember.sector_code == "S612"))
        session.execute(delete(SectorBasic).where(SectorBasic.code == "S612"))
        session.execute(delete(Watchlist).where(Watchlist.code == "S61201"))
        session.execute(delete(Watchlist).where(Watchlist.code == "600000"))
        session.execute(delete(StockEventCache).where(StockEventCache.code == "S61201"))
        session.execute(delete(IfindUsage).where(IfindUsage.server == "s612"))
        session.commit()


def _ensure_user(username: str) -> int:
    with SessionLocal() as session:
        row = session.scalar(select(AppUser).where(AppUser.username == username))
        if row is None:
            row = AppUser(username=username, password_hash="x", is_admin=False)
            session.add(row)
            session.commit()
            session.refresh(row)
        return row.id


@pytest.fixture(autouse=True)
def _isolate():
    _cleanup()
    with SessionLocal() as session:
        session.add(StockBasic(code="S61201", name="测试股"))
        session.add(SectorBasic(code="S612", name="测试板块", taxonomy="test"))
        session.commit()
    yield
    _cleanup()


def test_watchlist_sync_rejects_and_writes_nothing():
    before = _count(StockDaily)
    with pytest.raises(HTTPException) as info:
        watchlist_api.sync_watchlist(user=SimpleNamespace(id=1), days=250)
    assert info.value.status_code == 409
    assert _count(StockDaily) == before


def test_stock_sync_rejects_and_writes_nothing():
    with SessionLocal() as session:
        if session.get(StockBasic, "000001") is None:
            session.add(StockBasic(code="000001", name="平安银行"))
            session.commit()
    before = _count(StockDaily)
    with SessionLocal() as session:
        with pytest.raises(HTTPException) as info:
            stock_api.sync(code="000001", days=250, session=session, user=SimpleNamespace(id=1))
    assert info.value.status_code == 409
    assert _count(StockDaily) == before


def test_sector_members_get_is_read_only(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("GET 不得触发采集器")

    monkeypatch.setattr(sector_api, "_sector_collector", _boom)
    before = _count(SectorMember)
    rows, note = sector_api._board_members("S612", date(2026, 11, 13), 1)
    assert rows == []
    assert note is None or isinstance(note, str)
    assert _count(SectorMember) == before


def test_request_cannot_write_event_cache_but_background_can():
    with http_request_scope(allow_research_write=False):
        stock_events._write("S61201", "s612", [{"x": 1}])
    assert _count(StockEventCache) == 0

    stock_events._write("S61201", "s612", [{"x": 1}])
    assert _count(StockEventCache) == 1


def test_request_cannot_write_usage_but_background_can():
    with http_request_scope(allow_research_write=False):
        usage.record_call("s612", "t")
    assert _count(IfindUsage) == 0

    usage.record_call("s612", "t")
    assert _count(IfindUsage) == 1


def test_admin_request_scope_allows_research_write():
    with http_request_scope(allow_research_write=True):
        usage.record_call("s612", "t")
    assert _count(IfindUsage) == 1


def test_user_a_cannot_delete_user_b_watchlist():
    user_a = _ensure_user("s612a")
    user_b = _ensure_user("s612b")
    with SessionLocal() as session:
        session.add(Watchlist(user_id=user_b, code="600000", name="B 的票"))
        session.commit()

    with SessionLocal() as session:
        with pytest.raises(HTTPException) as info:
            watchlist_api.remove_watchlist("600000", user=SimpleNamespace(id=user_a), session=session)
    assert info.value.status_code == 404
    with SessionLocal() as session:
        row = session.get(Watchlist, (user_b, "600000"))
        assert row is not None
