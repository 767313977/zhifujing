"""盘前聚合（FEAT-PREMARKET）：降级路径与用户隔离。

钉四件事：
1. 非交易日照常返回（`is_trade_day=False`），不抛异常；
2. 竞价未采到时，竞价类字段一律 null、给「未采到」说明，涨停名单照常出；
3. 有竞价时状态标签 / 距关键价按口径算（高开、一字、相对关键价）；
4. 自选摘要只含当前用户。
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import (
    AppUser,
    AuctionSnapshot,
    Base,
    LimitPool,
    PatternHit,
    StockDaily,
    TradeCalendar,
    Watchlist,
)
from app.services import premarket

PREV = date(2026, 10, 29)
DAY = date(2026, 10, 30)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'premarket.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([TradeCalendar(trade_date=PREV), TradeCalendar(trade_date=DAY)])
        session.add_all(
            [
                LimitPool(
                    trade_date=PREV,
                    code="300001",
                    pool_type="up",
                    name="测试A",
                    consecutive=2,
                ),
                LimitPool(
                    trade_date=PREV,
                    code="300002",
                    pool_type="broken",
                    name="测试B",
                    consecutive=1,
                ),
                PatternHit(
                    trade_date=PREV,
                    code="300001",
                    pattern="wudao_sample",
                    name="测试A",
                    score=80.0,
                    key_levels={"watch_high": 11.0},
                    detail={},
                ),
                PatternHit(
                    trade_date=PREV,
                    code="300003",
                    pattern="wudao_start",
                    name="测试C",
                    score=70.0,
                    key_levels={"breakout": 20.0},
                    detail={"break_state": "收盘站稳"},
                ),
                StockDaily(
                    trade_date=PREV, code="300001", close=10.0, high=11.0, pct_chg=3.0
                ),
                StockDaily(
                    trade_date=PREV, code="300002", close=8.0, high=8.5, pct_chg=-2.0
                ),
            ]
        )
        user = AppUser(username="u1", password_hash="x")
        other = AppUser(username="u2", password_hash="x")
        session.add_all([user, other])
        session.flush()
        session.add_all(
            [
                Watchlist(user_id=user.id, code="300001", name="测试A"),
                Watchlist(user_id=other.id, code="600000", name="别人家的"),
            ]
        )
        session.commit()
        yield session, user.id, other.id


def test_non_trading_day_degrades(db):
    session, user_id, _ = db
    data = premarket.summary(session, date(2026, 10, 31), user_id)  # 周六
    assert data["is_trade_day"] is False
    assert data["auction_available"] is False
    assert "不是交易日" in (data["note"] or "")
    assert data["limit_up"] == []


def test_missing_auction_keeps_rows_but_nulls(db):
    session, user_id, _ = db
    data = premarket.summary(session, DAY, user_id)
    assert data["is_trade_day"] is True
    assert data["prev_trade_date"] == PREV
    assert data["auction_available"] is False
    assert "未采到" in (data["note"] or "")
    rows = {row["code"]: row for row in data["limit_up"]}
    assert set(rows) == {"300001", "300002"}
    assert rows["300001"]["auction_pct"] is None
    assert rows["300001"]["status"] is None
    # 悟道名单照常出，关键价来自昨日命中
    wudao = {row["code"]: row for row in data["wudao"]}
    assert wudao["300001"]["key_price"] == 11.0
    assert wudao["300001"]["vs_key_pct"] is None


def test_with_auction_computes_status_and_key_distance(db):
    session, user_id, _ = db
    session.add_all(
        [
            AuctionSnapshot(
                trade_date=DAY,
                code="300001",
                name="测试A",
                price=10.55,
                pre_close=10.0,
                pct_chg=5.5,
                open=10.55,
                post_open_price=10.4,
                limit_up=11.0,
            ),
            AuctionSnapshot(
                trade_date=DAY,
                code="300003",
                name="测试C",
                price=20.5,
                pre_close=18.0,
                pct_chg=13.89,
                open=20.5,
                limit_up=20.5,
            ),
        ]
    )
    session.commit()
    data = premarket.summary(session, DAY, user_id)
    rows = {row["code"]: row for row in data["limit_up"]}
    assert rows["300001"]["status"] == "高开"
    assert rows["300001"]["auction_pct"] == 5.5
    assert rows["300001"]["post_open_pct"] == 4.0
    assert rows["300002"]["status"] is None

    wudao = {row["code"]: row for row in data["wudao"]}
    assert wudao["300001"]["above_key"] is False
    assert wudao["300001"]["vs_key_pct"] == pytest.approx(-4.09, abs=0.01)
    assert wudao["300003"]["above_key"] is True
    assert wudao["300003"]["vs_key_pct"] == pytest.approx(2.5)


def test_watchlist_is_per_user(db):
    session, user_id, other_id = db
    mine = premarket.summary(session, DAY, user_id)
    theirs = premarket.summary(session, DAY, other_id)
    assert [row["code"] for row in mine["watchlist"]] == ["300001"]
    assert [row["code"] for row in theirs["watchlist"]] == ["600000"]
    assert mine["watchlist"][0]["close"] == 10.0


def test_future_date_raises(db):
    session, user_id, _ = db
    with pytest.raises(ValueError):
        premarket.summary(session, date(2099, 1, 1), user_id)
