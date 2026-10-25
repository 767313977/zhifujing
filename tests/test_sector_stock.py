"""板块 → 个股（选股池第四块）：强势/弱势板块里挑当日最强个股，以及次日验证。"""

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import (
    Base,
    SectorBasic,
    SectorDaily,
    SectorMember,
    StockBasic,
    StockDaily,
    TradeCalendar,
)
from app.services import pools

#: 20 个交易日（RPS20 的窗口），最后一天是信号日
DAYS = [date(2026, 9, 1) + timedelta(days=index) for index in range(20)]
SIGNAL = DAYS[-1]
NEXT = SIGNAL + timedelta(days=1)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _seed(session) -> None:
    """10 个板块，20 日涨幅各不相同：S00 最强（RPS20=95）、S09 最弱（=5）。"""
    session.add_all(TradeCalendar(trade_date=day) for day in (*DAYS, NEXT))
    for index in range(10):
        code = f"S{index:02d}"
        session.add(SectorBasic(code=code, name=code, taxonomy="kph_selected"))
        pct = 2.0 - index * 0.5  # +2.0 / +1.5 / … / −2.5
        for day in DAYS:
            session.add(
                SectorDaily(
                    trade_date=day,
                    sector_code=code,
                    name=code,
                    taxonomy="kph_selected",
                    pct_chg=pct,
                    member_count=3,
                )
            )
    # 强势板块 S00 的三只票：当日涨幅 1 / 5 / 3
    for code, pct in (("000001", 1.0), ("000002", 5.0), ("000003", 3.0)):
        session.add(StockBasic(code=code, name=code))
        session.add(
            SectorMember(
                trade_date=SIGNAL,
                sector_code="S00",
                code=code,
                name=code,
                pct_chg=pct,
            )
        )
    session.commit()


def test_sector_stock_picks_top_gain_in_strong_sector(session):
    _seed(session)

    board = pools.sector_stock(session, scope="strong", order="gain", sectors=1, per_sector=2)

    assert board.trade_date == SIGNAL
    assert board.conditions["scope"] == "strong"
    assert [item.code for item in board.sectors] == ["S00"]
    assert board.sectors[0].rps20 == pytest.approx(95.0)
    # 按当日涨幅降序取前 2：000002(+5%)、000003(+3%)
    assert [row.code for row in board.rows] == ["000002", "000003"]
    assert board.rows[0].sector_name == "S00"
    assert board.rows[0].factor_score is None  # 默认不算因子分


def test_sector_stock_weak_scope_is_the_control(session):
    _seed(session)

    board = pools.sector_stock(session, scope="weak", sectors=1, per_sector=3)

    assert [item.code for item in board.sectors] == ["S09"]
    assert board.sectors[0].rps20 == pytest.approx(5.0)
    # S09 没有成分股 —— 弱势档没票是正常的，不能拿别的板块补
    assert board.rows == []


def test_sector_stock_track_uses_next_day_quotes(session):
    _seed(session)
    for code in ("000001", "000002", "000003"):
        for day, close, pct in ((SIGNAL, 10.0, 0.0), (NEXT, 11.0, 10.0)):
            session.add(
                StockDaily(
                    trade_date=day,
                    code=code,
                    name=code,
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    pct_chg=pct,
                    amount=1e8,
                )
            )
    session.commit()

    track = pools.sector_stock_track(
        session, days=20, scope="strong", sectors=1, per_sector=2
    )

    assert track.conditions["scope"] == "strong"
    assert track.summary["verified_days"] == 1
    assert track.summary["samples"] == 2  # 000002、000003
    assert track.summary["ret1"]["mean"] == pytest.approx(10.0)
