"""S1-C：侧栏新增的只读个股接口（涨停记录 / 龙虎榜 / DDE 只读模式）。"""

from datetime import date

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.api import stock as stock_api
from app.models import Base, Lhb, LimitPool, StockBasic, StockDde
from app.services import stock_events  # noqa: F401  （确保模块可导入）


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(StockBasic(code="600000", name="浦发银行", industry="银行-股份行"))
        db.add_all(
            [
                LimitPool(
                    trade_date=date(2026, 11, 10),
                    code="600000",
                    pool_type="up",
                    name="浦发银行",
                    consecutive=2,
                    pct_chg=10.0,
                    price=10.4,
                    seal_amount=5e8,
                    first_seal_time="093000",
                    open_times=0,
                ),
                LimitPool(
                    trade_date=date(2026, 11, 3),
                    code="600000",
                    pool_type="broken",
                    name="浦发银行",
                    consecutive=1,
                    pct_chg=6.0,
                    price=10.1,
                ),
                LimitPool(
                    trade_date=date(2026, 11, 4),
                    code="600000",
                    pool_type="down",
                    name="浦发银行",
                ),
            ]
        )
        db.add_all(
            [
                Lhb(
                    trade_date=date(2026, 10, 30),
                    code="600000",
                    reason="日涨幅偏离值达7%",
                    name="浦发银行",
                    close=10.0,
                    pct_chg=8.0,
                    net_buy=3e7,
                ),
                Lhb(
                    trade_date=date(2026, 10, 20),
                    code="600000",
                    reason="连续三个交易日涨幅偏离值累计达20%",
                    name="浦发银行",
                    close=9.8,
                    pct_chg=5.0,
                    net_buy=-1e7,
                ),
            ]
        )
        db.add(
            StockDde(
                trade_date=date(2026, 11, 10),
                code="600000",
                net_inflow=1e8,
                dde=2e8,
            )
        )
        db.commit()
        yield db


def test_limit_history_is_read_only_and_desc(session):
    before = session.scalar(select(func.count()).select_from(LimitPool))
    rows = stock_api.limit_history("600000", limit=60, session=session)
    assert [row.trade_date for row in rows] == [date(2026, 11, 10), date(2026, 11, 3)]
    assert [row.pool_type for row in rows] == ["up", "broken"]
    assert rows[0].consecutive == 2
    assert session.scalar(select(func.count()).select_from(LimitPool)) == before


def test_stock_lhb_is_desc(session):
    rows = stock_api.stock_lhb("600000", limit=60, session=session)
    assert [row.trade_date for row in rows] == [date(2026, 10, 30), date(2026, 10, 20)]
    assert rows[0].net_buy == 3e7


def test_dde_live_false_never_calls_live_fetch(session, monkeypatch):
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("live 取数不该被调用")

    monkeypatch.setattr(stock_api, "_stock_dde", boom)
    user = type("U", (), {"id": 1})()
    out = stock_api.dde("600000", days=30, live=False, session=session, user=user)
    assert called["n"] == 0
    assert out.days == 1
    assert "只读" in (out.note or "")
