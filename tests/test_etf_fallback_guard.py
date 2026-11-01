"""ETF 采集的降级边界（2026-11-01 审计 P2-4）。

只有「交易所源坏了」（`EtfSourceError`）才回退 iFinD（约 59 次计费）；
本地写库错误（如 `database is locked`）必须原样上抛让步骤记 failed，
不能先烧一遍配额再在同一个错误上失败。
"""

from datetime import date

import pytest

from app.jobs import collect_funds
from app.sources import etf_exchange

TODAY = date(2026, 11, 2)


def _noop_ifind(*args, **kwargs) -> int:
    return 99


def test_exchange_source_error_falls_back_to_ifind(monkeypatch):
    calls: list[str] = []

    def _boom(target):
        raise etf_exchange.EtfSourceError("网络断了")

    monkeypatch.setattr(collect_funds, "_collect_etf_exchange", _boom)
    monkeypatch.setattr(
        collect_funds,
        "_collect_etf_ifind",
        lambda ak, ifind: calls.append("ifind") or 59,
    )
    # 目标日 = 今天才可能走交易所源（历史日会直接 EtfNotReady）
    monkeypatch.setattr(collect_funds, "today_cn", lambda: TODAY)

    assert collect_funds.collect_etf(None, None, TODAY) == 59
    assert calls == ["ifind"]


def test_local_database_error_is_not_swallowed(monkeypatch):
    def _db_locked(target):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(collect_funds, "_collect_etf_exchange", _db_locked)
    monkeypatch.setattr(
        collect_funds, "_collect_etf_ifind", lambda ak, ifind: pytest.fail("不该回退 iFinD")
    )
    monkeypatch.setattr(collect_funds, "today_cn", lambda: TODAY)

    with pytest.raises(RuntimeError, match="database is locked"):
        collect_funds.collect_etf(None, None, TODAY)


def test_historical_date_does_not_fall_back(monkeypatch):
    """历史日期：交易所没有快照 → EtfNotReady（调用方记 skipped），不回退 iFinD。"""
    monkeypatch.setattr(
        collect_funds, "_collect_etf_ifind", lambda ak, ifind: pytest.fail("不该回退 iFinD")
    )
    monkeypatch.setattr(collect_funds, "today_cn", lambda: TODAY)

    with pytest.raises(etf_exchange.EtfNotReady):
        collect_funds.collect_etf(None, None, date(2026, 10, 30))
