"""持仓（FEAT-POSITIONS）：复用 trade_journal 未平仓记录，用户隔离。"""

from datetime import date

import pytest
from sqlalchemy import create_engine, select
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


def test_pnl_uses_buy_day_basis_after_split():
    """10 送 10 之后不能显示假亏损：浮盈要按买入日口径（与交易日志一致）。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    entry = date(2026, 10, 28)
    split = date(2026, 10, 29)
    with Session(engine) as session:
        session.add_all(
            [
                StockBasic(code="300001", name="测试A"),
                # 买入日 10 元；次日 10 送 10 → 不复权收盘 5 元，真实收益 0
                StockDaily(trade_date=entry, code="300001", close=10.0, pct_chg=0.0),
                StockDaily(trade_date=split, code="300001", close=5.0, pct_chg=0.0),
            ]
        )
        session.add(AppUser(username="u1", password_hash="x"))
        session.commit()
        user = session.scalar(select(AppUser))
        journal = positions.create(
            session,
            user.id,
            code="300001",
            entry_date=entry,
            entry_price=10.0,
            shares=1000,
        )
        row = {item["id"]: item for item in positions.load(session, user.id)}[journal.id]
        assert row["latest_close"] == 5.0  # 行情口径仍是不复权价
        assert row["pnl_pct"] == 0.0
        assert row["pnl"] == 0.0
        assert row["market_value"] == 5000.0


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
            code="300001",
            entry_date=DAY,
            entry_price=12.0,
            shares=200,
            position_pct=30,
            note="加仓",
        ),
        session,
        user,
    )
    assert updated.entry_price == 12.0
    assert updated.shares == 200
    assert updated.note == "加仓"
    assert updated.position_pct == 30
    # 编辑时清空是可预期的：仓位比例、备注传 null 就真的清掉
    cleared = positions_api.update_position(
        row.id,
        PositionIn(code="300001", entry_date=DAY, entry_price=12.0, shares=200),
        session,
        user,
    )
    assert cleared.position_pct is None
    assert cleared.note is None
    assert positions_api.delete_position(row.id, session, user)["ok"] is True
    assert positions.load(session, user.id) == []
