"""持仓（FEAT-POSITIONS）：复用 trade_journal 未平仓记录，用户隔离。"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api import positions as positions_api
from app.models import AppUser, Base, StockBasic, StockDaily, TradeJournal, TradePlan
from app.schemas import PositionIn
from app.services import positions

DAY = date(2026, 10, 30)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'positions.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                StockBasic(code="300001", name="测试A"),
                StockDaily(trade_date=DAY, code="300001", close=11.0, pct_chg=2.0),
            ]
        )
        mine = AppUser(username="u1", password_hash="x")
        other = AppUser(username="u2", password_hash="x")
        session.add_all([mine, other])
        session.flush()
        session.add(
            TradeJournal(
                user_id=other.id,
                code="300001",
                entry_date=DAY,
                entry_price=10.0,
                exit_date=None,
            )
        )
        session.commit()
        yield session, mine, other


def test_create_and_load_pnl(db):
    session, user, _ = db
    journal = positions.create(
        session, user.id, code="300001", entry_date=DAY, entry_price=10.0, shares=1000
    )
    rows = positions.load(session, user.id)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == journal.id
    assert row["latest_close"] == 11.0
    assert row["pnl"] == 1000.0
    assert row["pnl_pct"] == 10.0
    # 别人的未平仓记录不出现
    assert all(item["id"] != journal.id for item in positions.load(session, 999))


def test_from_plan_marks_executed(db):
    session, user, _ = db
    plan = TradePlan(
        user_id=user.id,
        code="300001",
        name="测试A",
        plan_date=DAY,
        buy_price=10.5,
        stop_price=9.5,
        status_source="auto",
    )
    session.add(plan)
    session.commit()

    row = positions_api.create_from_plan(plan.id, session, user)
    assert row.entry_price == 10.5
    assert row.plan_id == plan.id
    refreshed = session.get(TradePlan, plan.id)
    assert refreshed.status == "executed"
    assert refreshed.actual_price == 10.5


def test_update_and_delete_only_open(db):
    session, user, _ = db
    row = positions_api.create_position(
        PositionIn(code="300001", entry_date=DAY, entry_price=10.0, shares=100),
        session,
        user,
    )
    updated = positions_api.update_position(
        row.id,
        PositionIn(
            code="300001", entry_date=DAY, entry_price=12.0, shares=200, note="加仓"
        ),
        session,
        user,
    )
    assert updated.entry_price == 12.0
    assert updated.shares == 200
    assert updated.note == "加仓"
    assert positions_api.delete_position(row.id, session, user)["ok"] is True
    assert positions.load(session, user.id) == []
