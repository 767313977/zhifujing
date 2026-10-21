"""交易日志的按用户隔离：别人（包括管理员）看不到、也改不了我的记录。

2026-10-21 用户要求：「每个人只能查看自己，我作为管理员也无法查看其他人的页面数据」。
这里把边界钉住，以后加接口时如果漏了 `user_id` 过滤，这几条会直接失败。
"""

from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.api.journal import (
    create_t,
    delete_t,
    delete_trade,
    list_trades,
    update_t,
    update_trade,
)
from app.models import AppUser, Base, TradeCalendar, TradeJournal, TradeT
from app.schemas import TradeJournalIn, TradeTIn


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", future=True)

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        admin = AppUser(username="admin", password_hash="x", is_admin=True)
        owner = AppUser(username="owner", password_hash="x")
        other = AppUser(username="other", password_hash="x")
        session.add_all([admin, owner, other])
        for day in (date(2026, 8, 3), date(2026, 8, 4)):
            session.add(TradeCalendar(trade_date=day))
        session.commit()
        yield session, admin, owner, other


def _trade(session: Session, user_id: int) -> TradeJournal:
    row = TradeJournal(
        user_id=user_id,
        code="600519",
        name="贵州茅台",
        entry_date=date(2026, 8, 3),
        entry_price=10.0,
        reason_type="other",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _trade_payload() -> TradeJournalIn:
    return TradeJournalIn(
        code="600519",
        entry_date=date(2026, 8, 3),
        entry_price=11.0,
        reason_type="other",
    )


def _t_payload() -> TradeTIn:
    return TradeTIn(
        trade_date=date(2026, 8, 4),
        leg2_date=date(2026, 8, 4),
        direction="long",
        buy_price=10.1,
        sell_price=10.4,
        ratio_pct=33.0,
        trigger="other",
    )


def test_list_only_returns_own_rows(db):
    session, admin, owner, other = db
    mine = _trade(session, owner.id)

    assert [row.id for row in list_trades(session=session, user=owner).rows] == [
        mine.id
    ]
    assert list_trades(session=session, user=other).rows == []
    # 管理员也一样：他名下没有记录，就一条都看不到
    assert list_trades(session=session, user=admin).rows == []


def test_other_user_and_admin_cannot_edit_or_delete(db):
    session, admin, owner, other = db
    mine = _trade(session, owner.id)

    for stranger in (other, admin):
        with pytest.raises(HTTPException) as exc:
            update_trade(mine.id, _trade_payload(), session=session, user=stranger)
        assert exc.value.status_code == 404

        with pytest.raises(HTTPException) as exc:
            delete_trade(mine.id, session=session, user=stranger)
        assert exc.value.status_code == 404

    # 别人的失败请求不能真的把记录改掉 / 删掉
    session.refresh(mine)
    assert mine.entry_price == 10.0


def test_t_endpoints_are_scoped(db):
    session, admin, owner, other = db
    mine = _trade(session, owner.id)
    t_row = TradeT(
        user_id=owner.id,
        trade_id=mine.id,
        trade_date=date(2026, 8, 4),
        leg2_date=date(2026, 8, 4),
        direction="long",
        buy_price=10.0,
        sell_price=10.3,
        ratio_pct=33.0,
        trigger="other",
    )
    session.add(t_row)
    session.commit()
    session.refresh(t_row)

    for stranger in (other, admin):
        with pytest.raises(HTTPException) as exc:
            create_t(mine.id, _t_payload(), session=session, user=stranger)
        assert exc.value.status_code == 404

        with pytest.raises(HTTPException) as exc:
            update_t(t_row.id, _t_payload(), session=session, user=stranger)
        assert exc.value.status_code == 404

        with pytest.raises(HTTPException) as exc:
            delete_t(t_row.id, session=session, user=stranger)
        assert exc.value.status_code == 404

    assert session.get(TradeT, t_row.id) is not None
