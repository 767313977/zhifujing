"""S4-D：外部数据现取的离线闸门与 egress 计数。

关键断言：开关关闭时**真正发网次数必须为 0**，而不是「发出去了但失败了」。
"""

from datetime import timedelta

import pytest
import requests
from sqlalchemy import delete

from app import external_guard as eg
from app.api import sector as sector_api
from app.api import stock as stock_api
from app.db import SessionLocal
from app.models import SectorBasic, SectorMember, TradeCalendar
from app.sources.base import retry_call
from app.sources.kaipanhong import KaipanhongSource
from app.timeutil import today_cn


@pytest.fixture(autouse=True)
def _restore_guard(monkeypatch):
    eg.uninstall_network_deny()
    eg.reset_stats()
    monkeypatch.delenv(eg.ENV, raising=False)
    yield
    eg.uninstall_network_deny()
    eg.reset_stats()


def _offline(monkeypatch):
    monkeypatch.setenv(eg.ENV, "false")


def test_enabled_by_default():
    assert eg.enabled() is True
    eg.ensure_external_fetch("测试")  # 不应抛
    assert eg.stats()["attempts"] == 1


def test_ensure_blocks_when_disabled(monkeypatch):
    _offline(monkeypatch)
    assert eg.enabled() is False
    with pytest.raises(eg.ExternalFetchBlocked):
        eg.ensure_external_fetch("测试来源")
    stats = eg.stats()
    assert stats["attempts"] == 1
    assert stats["blocked"] == 1
    assert stats["network_attempts"] == 0


def test_retry_call_does_not_retry_blocked(monkeypatch):
    _offline(monkeypatch)
    with pytest.raises(eg.ExternalFetchBlocked):
        retry_call(lambda: eg.ensure_external_fetch("测试"), retries=3)
    # 只尝试一次：不会白等 2s + 4s 的退避
    assert eg.stats()["attempts"] == 1


def test_network_deny_blocks_real_requests(monkeypatch):
    _offline(monkeypatch)
    assert eg.install_network_deny() is True
    with pytest.raises(eg.ExternalFetchBlocked):
        requests.get("https://example.com/never", timeout=1)
    stats = eg.stats()
    assert stats["network_blocked"] == 1
    assert stats["network_attempts"] == 0


def test_kaipanhong_source_is_blocked_before_network(monkeypatch):
    _offline(monkeypatch)
    with pytest.raises(eg.ExternalFetchBlocked):
        KaipanhongSource().board_members(today_cn(), "801001")
    stats = eg.stats()
    assert stats["blocked"] == 1
    assert stats["network_attempts"] == 0


def test_board_members_falls_back_without_network(monkeypatch):
    _offline(monkeypatch)
    today = today_cn()
    with SessionLocal() as db:
        for offset in range(10):
            db.merge(TradeCalendar(trade_date=today - timedelta(days=offset)))
        db.merge(SectorBasic(code="T99", name="离线测试板块", taxonomy="kph_selected"))
        db.execute(delete(SectorMember).where(SectorMember.sector_code == "T99"))
        db.commit()

    eg.install_network_deny()
    rows, note = sector_api._board_members("T99", today, 1)
    assert rows == []
    assert note and "离线验收模式" in note
    stats = eg.stats()
    assert stats["network_attempts"] == 0
    assert stats["network_blocked"] == 0


def test_stock_dde_returns_cache_without_network(monkeypatch):
    _offline(monkeypatch)
    eg.install_network_deny()
    rows, note = stock_api._stock_dde("600000", 60, 1)
    assert rows == []
    assert note and "离线验收模式" in note
    assert eg.stats()["network_attempts"] == 0


def test_env_flag_does_not_reuse_scheduler_flag(monkeypatch):
    # 明确验证两者语义独立：只关调度器不该关外呼闸门
    monkeypatch.delenv(eg.ENV, raising=False)
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    assert eg.enabled() is True
