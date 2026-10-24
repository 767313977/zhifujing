"""RPS 扫描的三条硬口径（2026-10-24「选股池-2」）：

1. 窗口按**交易日**截取，不是「最近 N 行」；
2. 当天日线覆盖率不到 90% 时**不写库**；
3. 当天没有日线的票不进当天的横截面排名。

`scan()` 用的是模块级 `session_scope`，测试照 `test_collect_activity` 的写法
换成内存库。
"""

from contextlib import contextmanager
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.jobs import scan_rps
from app.models import Base, RpsDaily, StockDaily, StockUniverse, TradeCalendar

# 从 2026-01-05（周一）起连续排 250 个「交易日」，实际跳过周末 —— 只用来看窗口，
# 不需要真的是交易所日历
START = date(2026, 1, 5)


def _days(count: int) -> list[date]:
    out: list[date] = []
    cursor = START
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


@pytest.fixture()
def engine():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return engine


def _patch_scope(monkeypatch, engine) -> None:
    @contextmanager
    def scope():
        with Session(engine) as session:
            yield session
            session.commit()

    monkeypatch.setattr(scan_rps, "session_scope", scope)


def _seed(session: Session, days: list[date], code: str, pct: float, skip: set[int]) -> None:
    """给一只票铺日线，`skip` 里的下标不铺（模拟停牌 / 漏采）。"""
    for index, day in enumerate(days):
        if index in skip:
            continue
        session.add(
            StockDaily(
                trade_date=day,
                code=code,
                name=code,
                close=10.0,
                pct_chg=pct,
            )
        )


def test_window_is_trading_days_not_rows(engine, monkeypatch):
    """中间缺 10 行时，`ret120` 只复利窗口内的行（不是「最近 120 行」）。"""
    days = _days(130)
    target = days[-1]
    # 缺 10 天（下标 60~69）→ 总共 120 行，但落在 120 交易日窗口内只有 110 行
    missing = set(range(60, 70))
    with Session(engine) as session:
        session.add_all(TradeCalendar(trade_date=day) for day in days)
        _seed(session, days, "000001", 1.0, missing)
        session.commit()

    _patch_scope(monkeypatch, engine)
    assert scan_rps.scan(target)["rows"] == 1

    with Session(engine) as session:
        row = session.scalar(select(RpsDaily).where(RpsDaily.code == "000001"))
    # 旧写法取「最近 120 行」会把窗口外的 10 行也算进来 → 1.01**120
    assert row.ret120 == pytest.approx(round((1.01**110 - 1) * 100, 2))
    # 只有 130 个交易日，250 日窗口覆盖不足 → 该档记 None
    assert row.ret250 is None
    assert row.rps250 is None


def test_skips_when_day_coverage_too_low(engine, monkeypatch):
    """当天只有一半的票有日线 → 不写库。"""
    days = _days(130)
    target = days[-1]
    with Session(engine) as session:
        session.add_all(TradeCalendar(trade_date=day) for day in days)
        # 池子 100 只，只有 50 只有当天日线（另 50 只连历史都没有）
        session.add_all(
            StockUniverse(code=f"000{index:03d}") for index in range(100)
        )
        for index in range(50):
            _seed(session, days, f"000{index:03d}", 1.0, set())
        session.commit()

    _patch_scope(monkeypatch, engine)
    result = scan_rps.scan(target)
    assert result == {
        "rows": 0,
        "trade_date": target,
        "skipped": "coverage",
        "cost_seconds": result["cost_seconds"],
    }
    with Session(engine) as session:
        assert session.scalar(select(RpsDaily).limit(1)) is None


def test_stock_without_target_day_is_excluded(engine, monkeypatch):
    """当天停牌的票不进当天排名 —— 收益截止在不同日子没法比。"""
    days = _days(130)
    target = days[-1]
    with Session(engine) as session:
        session.add_all(TradeCalendar(trade_date=day) for day in days)
        _seed(session, days, "000001", 1.0, set())
        # 000002 历史够，但最后 3 天没有行（停牌）
        _seed(session, days, "000002", 1.0, {127, 128, 129})
        session.commit()

    _patch_scope(monkeypatch, engine)
    assert scan_rps.scan(target)["rows"] == 1

    with Session(engine) as session:
        codes = list(session.scalars(select(RpsDaily.code)))
    assert codes == ["000001"]
