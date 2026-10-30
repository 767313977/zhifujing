"""可转债与个股联动（可转债-5）。

钉四件事：

1. 个股页「存续转债」只列未摘牌的券，并给出状态文案；
2. 自选可以放转债代码：名称取 `cb_basic`、行情取 `cb_daily`；
3. 简报的「转债强赎提醒」只在**状态切换那天**出现，且今天快照没采到时就整节省略；
4. 已公告强赎的券不会被误当成「今天新公告」重复提醒。
"""

from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api import watchlist as watchlist_api
from app.models import AppUser, Base, CbBasic, CbDaily, Watchlist
from app.schemas import WatchlistIn
from app.services import brief_watchlist, convertible

DAY = date(2026, 10, 30)
PREV = date(2026, 10, 29)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cb-link.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(AppUser(username="admin", password_hash="x", is_admin=True))
        session.commit()
        yield session


def add_bond(session, code, stock_code="601229", **kwargs):
    fields = dict(
        bond_code=code,
        bond_name=f"{code}转债",
        stock_code=stock_code,
        stock_name="上海银行",
        maturity_date=date(2027, 1, 25),
    )
    fields.update(kwargs)
    session.add(CbBasic(**fields))


def add_daily(session, day, code, **kwargs):
    fields = dict(
        trade_date=day,
        bond_code=code,
        close=118.0,
        pct_chg=1.2,
        premium_pct=8.5,
        conv_value=110.0,
        redeem_status=None,
    )
    fields.update(kwargs)
    session.add(CbDaily(**fields))


def _seed(session) -> None:
    add_bond(session, "113042")
    # 已摘牌：个股页与提醒都不该再出现
    add_bond(session, "123001", delist_date=date(2026, 10, 1))
    # 别的正股的券：不该被 601229 的卡片带出来
    add_bond(session, "127999", stock_code="000001", stock_name="平安银行")
    add_daily(session, DAY, "113042", redeem_status="已公告强赎", last_trade_date=date(2026, 11, 13))
    add_daily(session, PREV, "113042", redeem_status=None)
    session.commit()


def test_bonds_of_stock_skips_delisted_and_labels_state(db):
    _seed(db)

    bonds = convertible.bonds_of_stock(db, "601229")

    assert [row.bond_code for row in bonds] == ["113042"]
    assert bonds[0].close == 118.0
    assert bonds[0].state == "已公告强赎"


def test_bonds_of_stock_empty_for_unknown_stock(db):
    _seed(db)
    assert convertible.bonds_of_stock(db, "600000") == []


def test_state_text_for_near_redeem_uses_required_days(db):
    daily = CbDaily(
        trade_date=DAY,
        bond_code="113042",
        redeem_days=12,
        redeem_days_required=15,
    )
    assert convertible._state_text(daily) == "强赎临近 12/15"


def test_watchlist_accepts_bond_code_and_reads_cb_daily(db):
    _seed(db)
    user = db.scalars(select(AppUser)).one()

    row = watchlist_api.add_watchlist(
        payload=WatchlistIn(code="113042"), user=user, session=db
    )

    assert row.code == "113042"
    assert row.name == "113042转债"
    assert row.latest_date == DAY
    assert row.close == 118.0
    assert row.pct_chg == 1.2


def test_watchlist_still_rejects_unknown_code(db):
    _seed(db)
    user = db.scalars(select(AppUser)).one()
    with pytest.raises(HTTPException):
        watchlist_api.add_watchlist(
            payload=WatchlistIn(code="999999"), user=user, session=db
        )


def test_redeem_block_alerts_on_transition_only(db):
    _seed(db)
    user = db.scalars(select(AppUser)).one()
    db.add(Watchlist(user_id=user.id, code="113042", name="113042转债"))
    # 另一只：昨天就已经是「已公告强赎」，今天不该再提醒
    add_bond(db, "110001")
    add_daily(db, PREV, "110001", redeem_status="已公告强赎")
    add_daily(db, DAY, "110001", redeem_status="已公告强赎")
    db.add(Watchlist(user_id=user.id, code="110001", name="110001转债"))
    db.commit()

    text = brief_watchlist.redeem_block(db, DAY)

    assert "转债强赎提醒" in text
    assert "113042" in text
    assert "已公告强赎" in text
    assert "最后交易日 2026-11-13" in text
    assert "110001" not in text


def test_redeem_block_follows_watched_stock(db):
    """自选的是正股（不是转债代码）也要提醒 —— 持仓常常是股票。"""
    _seed(db)
    user = db.scalars(select(AppUser)).one()
    db.add(Watchlist(user_id=user.id, code="601229", name="上海银行"))
    db.commit()

    text = brief_watchlist.redeem_block(db, DAY)

    assert "113042" in text


def test_redeem_block_skips_when_snapshot_missing(db):
    _seed(db)
    user = db.scalars(select(AppUser)).one()
    db.add(Watchlist(user_id=user.id, code="113042", name="113042转债"))
    db.commit()

    # 下一个交易日还没采到快照：不能拿昨天的状态冒充「今天新公告」
    assert brief_watchlist.redeem_block(db, date(2026, 11, 2)) == ""
