"""板块 RPS：只用 `sector_daily`（板块指数自己带历史），不拼成分。"""

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, SectorDaily, TradeCalendar
from app.services import sector_rps

START = date(2026, 8, 3)


def _days(count: int) -> list[date]:
    return [START + timedelta(days=index) for index in range(count)]


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _seed(session, days: list[date], series: dict[str, dict[str, object]]) -> None:
    """`series = {板块代码: {pct, name, members}}`，按天铺板块日线。"""
    session.add_all(TradeCalendar(trade_date=day) for day in days)
    for code, config in series.items():
        for day in days:
            session.add(
                SectorDaily(
                    trade_date=day,
                    sector_code=code,
                    name=config["name"],
                    taxonomy="kph_selected",
                    pct_chg=config["pct"],
                    member_count=config["members"],
                    leader_name=f"{config['name']}龙一",
                    leader_pct_chg=10.0,
                )
            )
    session.commit()


def test_board_ranks_by_20_day_return(session):
    days = _days(25)
    _seed(
        session,
        days,
        {
            "A": {"pct": 1.0, "name": "强板块", "members": 30},
            "B": {"pct": 0.5, "name": "中板块", "members": 20},
            "C": {"pct": 0.0, "name": "弱板块", "members": 10},
        },
    )

    board = sector_rps.board(session, taxonomy="kph_selected")
    assert board.trade_date == days[-1]
    assert [row.code for row in board.rows] == ["A", "B", "C"]
    assert board.rows[0].rps20 == 83.3
    assert board.rows[1].rps20 == 50.0
    # 当天那一行的展示信息挂在行上
    assert board.rows[0].name == "强板块"
    assert board.rows[0].member_count == 30
    assert board.rows[0].leader_name == "强板块龙一"


def test_board_skips_other_taxonomy(session):
    days = _days(25)
    _seed(session, days, {"A": {"pct": 1.0, "name": "精选", "members": 5}})
    session.add_all(
        SectorDaily(
            trade_date=day,
            sector_code="I1",
            name="行业",
            taxonomy="kph_industry",
            pct_chg=2.0,
        )
        for day in days
    )
    session.commit()

    board = sector_rps.board(session, taxonomy="kph_industry")
    assert [row.code for row in board.rows] == ["I1"]
    assert board.rows[0].name == "行业"


def test_track_compares_top_sectors_with_all(session):
    """最强板块继续强 → 超额为正；最后几天没有持有期数据 → pending。"""
    days = _days(45)
    _seed(
        session,
        days,
        {
            "A": {"pct": 1.0, "name": "强板块", "members": 30},
            "B": {"pct": 0.0, "name": "中板块", "members": 20},
            "C": {"pct": -1.0, "name": "弱板块", "members": 10},
        },
    )

    track = sector_rps.track(session, days=20, top=1, taxonomy="kph_selected")
    assert track.summary["days"] == 20
    # 只有前面 15 天左右能算完 5 日持有期
    assert 0 < track.summary["verified_days"] < 20
    assert track.summary["ret5"]["samples"] > 0
    assert track.summary["ret5"]["excess"] > 0
    assert [row.status for row in track.days].count("pending") > 0


def test_board_without_data_returns_empty(session):
    board = sector_rps.board(session)
    assert board.trade_date is None
    assert board.rows == []
    assert sector_rps.track(session).summary == {}
