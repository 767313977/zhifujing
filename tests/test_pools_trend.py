"""趋势模板的「预算一份序列再切片」路径（`rps/track` 用）结果必须与逐日现读一致。"""

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, RpsDaily, StockBasic, StockDaily, TradeCalendar
from app.services import pools

START = date(2025, 9, 1)
DAYS = [START + timedelta(days=index) for index in range(270)]
DAY = DAYS[-1]


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _seed(session, code: str, pct: float) -> None:
    session.add(StockBasic(code=code, name=code))
    for day in DAYS:
        session.add(
            StockDaily(
                trade_date=day,
                code=code,
                name=code,
                open=10.0,
                high=10.2,
                low=9.8,
                close=10.0,
                volume=1e6,
                amount=1e8,
                pct_chg=pct,
            )
        )
    session.add(
        RpsDaily(
            trade_date=DAY,
            code=code,
            ret50=10.0,
            ret120=20.0,
            ret250=30.0,
            rps50=90.0,
            rps120=95.0,
            rps250=100.0,
        )
    )


def test_trend_cache_matches_fresh_read(session):
    """预加载切片与「每只票现读 260 天」必须选出同一批票。"""
    session.add_all(TradeCalendar(trade_date=day) for day in DAYS)
    _seed(session, "000001", pct=0.5)  # 稳定上涨 → 满足趋势模板
    _seed(session, "000002", pct=-0.5)  # 阴跌 → 不满足
    session.commit()

    plain = pools.rps_board(session, trade_date=DAY, limit=100, trend_template=True)
    cache = pools._build_trend_bars(session, [DAY])
    cached = pools.rps_board(
        session, trade_date=DAY, limit=100, trend_template=True, trend_cache=cache
    )

    assert [row.code for row in plain.rows] == ["000001"]
    assert [row.code for row in cached.rows] == [row.code for row in plain.rows]
