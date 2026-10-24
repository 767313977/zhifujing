"""次日验证面板（选股池-4）：晋级率、开盘溢价、一字买不进、待验证。"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import (
    Base,
    LimitPool,
    RpsDaily,
    StockBasic,
    StockDaily,
    TradeCalendar,
)
from app.services import pools

D0 = date(2026, 10, 20)  # 信号日
D1 = date(2026, 10, 21)  # 次日（有日线）
D2 = date(2026, 10, 22)  # 另一信号日
D3 = date(2026, 10, 23)  # 次日**还没采到**


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _limit(session, day: date, code: str, pool_type: str = "up", **kwargs) -> None:
    base = {
        "trade_date": day,
        "code": code,
        "pool_type": pool_type,
        "consecutive": 1,
        "open_times": 0,
        "first_seal_time": "093000",
        "float_mv": 50 * pools.YI,
        "amount": 8 * pools.YI,
        "seal_amount": 5 * pools.YI,
        "turnover": 10.0,
        "pct_chg": 10.0,
        "price": 10.0,
    }
    base.update(kwargs)
    session.add(LimitPool(**base))


def _daily(session, day: date, code: str, **kwargs) -> None:
    base = {
        "trade_date": day,
        "code": code,
        "name": code,
        "open": 10.0,
        "high": 10.5,
        "low": 9.8,
        "close": 10.0,
        "pct_chg": 0.0,
    }
    base.update(kwargs)
    session.add(StockDaily(**base))


def test_first_board_track_counts_promotion_and_unbuyable(session):
    session.add_all(TradeCalendar(trade_date=day) for day in (D0, D1, D2, D3))
    for code in ("000001", "000002", "000003"):
        session.add(StockBasic(code=code, name=code))
    # D0 两只首板候选；D2 还有一只（次日日线没采到 → 待验证）
    _limit(session, D0, "000001")
    _limit(session, D0, "000002")
    _limit(session, D2, "000003")

    # 000001 次日一字涨停：开=高=低=收、且在涨停池里 → 晋级但买不进
    _daily(session, D1, "000001", open=11.0, high=11.0, low=11.0, close=11.0, pct_chg=10.0)
    # 连板 2 → 次日自己不再是「首板候选」（只用来判它晋级）
    _limit(session, D1, "000001", consecutive=2)
    # 000002 次日未晋级：昨收 = 10.8 / 1.08 = 10，开盘 10.5 → 溢价 +5%，收盘 +8%
    _daily(session, D1, "000002", open=10.5, high=11.0, low=10.2, close=10.8, pct_chg=8.0)

    session.commit()

    track = pools.first_board_track(session, days=20, min_themes=0)
    summary = track.summary

    # D0 已验证；D1 当天没有首板候选（000001 已是 2 连板）、但次日日线在，也算已验证
    assert summary["verified_days"] == 2
    assert summary["pending_days"] == 1
    # 样本只算「买得到」的：000001 一字买不进，000002 可买
    assert summary["samples"] == 1
    assert summary["candidates"] == 2
    assert summary["promoted_rate"] == 50.0
    assert summary["unbuyable_rate"] == 50.0
    assert summary["open_premium_mean"] == 5.0
    assert summary["close_ret_mean"] == 8.0
    assert summary["limit_down_rate"] == 0.0

    pending = [row for row in track.days if row.status == "pending"]
    assert [row.trade_date for row in pending] == [D2]
    assert pending[0].next_date == D3


def test_first_board_track_marks_big_loss_and_limit_down(session):
    session.add_all(TradeCalendar(trade_date=day) for day in (D0, D1))
    session.add(StockBasic(code="000001", name="000001"))
    _limit(session, D0, "000001")
    # 次日跌停（-10%）→ 大面 + 跌停各计一次
    _daily(session, D1, "000001", open=9.5, high=9.5, low=9.0, close=9.0, pct_chg=-10.0)
    _limit(session, D1, "000001", pool_type="down")
    session.commit()

    summary = pools.first_board_track(session, days=20, min_themes=0).summary
    assert summary["big_loss_rate"] == 100.0
    assert summary["limit_down_rate"] == 100.0
    # 昨收 = 9.0 / 0.9 = 10，开盘 9.5 → 溢价 -5%
    assert summary["open_premium_mean"] == -5.0


def test_rps_track_uses_equal_weight_market(session):
    days = [date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14),
            date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 19)]
    session.add_all(TradeCalendar(trade_date=day) for day in days)
    for code in ("000001", "000002"):
        session.add(StockBasic(code=code, name=code))
        session.add(
            RpsDaily(
                trade_date=days[0],
                code=code,
                ret120=30.0,
                ret250=60.0,
                rps120=95.0,
                rps250=99.0,
            )
        )
        for day in days:
            _daily(session, day, code, close=10.0, pct_chg=1.0)
    session.commit()

    track = pools.rps_track(session, days=20, min_rps=0, top=50, mode="all")
    assert track.summary["days"] == 1
    assert track.summary["samples"] == 2
    # 每天 +1%：1 日 +1.00%，5 日 1.01**5−1 = +5.10%
    assert track.summary["ret1"]["mean"] == 1.0
    assert track.summary["ret5"]["mean"] == 5.1
    # 样本就是全市场本身 → 超额为 0
    assert track.summary["ret1"]["excess"] == 0.0
    assert track.summary["ret5"]["excess"] == 0.0


def test_rps_track_without_data_is_empty(session):
    track = pools.rps_track(session)
    assert track.summary["days"] == 0
    assert track.summary["ret1"]["mean"] is None
