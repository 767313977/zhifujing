"""做 T 记录：指标口径（纯函数）+ 校验与级联删除（内存 SQLite）。

口径见 2026-10-21《做 T 记录需求》§3 / §6。
"""

from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from app.api.journal import _apply_t, _check_t_conflicts
from app.models import (
    AppUser,
    Base,
    TradeCalendar,
    TradeJournal,
    TradeT,
)
from app.schemas import TradeTIn
from app.services.trade_journal import (
    _adjust,
    _t_group_stats,
    _t_metrics,
    _window,
)

ENTRY = date(2026, 8, 3)
DAYS = [
    date(2026, 8, 3),
    date(2026, 8, 4),
    date(2026, 8, 5),
    date(2026, 8, 6),
    date(2026, 8, 7),
]


def _trade(**overrides) -> TradeJournal:
    values = {
        "user_id": 1,
        "code": "600519",
        "entry_date": ENTRY,
        "entry_price": 10.0,
        "reason_type": "other",
    }
    values.update(overrides)
    return TradeJournal(**values)


def _t(**overrides) -> TradeT:
    values = {
        "user_id": 1,
        "trade_id": 1,
        "trade_date": date(2026, 8, 4),
        "direction": "long",
        "buy_price": 10.0,
        "sell_price": None,
        "ratio_pct": 33.0,
        "trigger": "other",
    }
    values.update(overrides)
    return TradeT(**values)


def _metrics(trade: TradeJournal, rows: list[TradeT], bars, data_end, ranges=None):
    window = _window(trade, bars, data_end)
    adjusted = _adjust(window) if window else []
    return _t_metrics(trade, rows, window, adjusted, ranges or {})


def test_long_completed():
    """正 T 已完成：买 10.00、卖 10.30、比例 33、底仓成本 10.00。"""
    bars = [
        (date(2026, 8, 3), 10.0, 0.0),
        (date(2026, 8, 4), 10.0, 0.0),
        (date(2026, 8, 5), 10.0, 0.0),
    ]
    out = _metrics(
        _trade(exit_date=None),
        [_t(leg2_date=date(2026, 8, 5), sell_price=10.3)],
        bars,
        date(2026, 8, 5),
    )[0]
    assert out["completed"] is True
    assert out["spread_pct"] == pytest.approx(3.0, abs=0.01)
    assert out["net_pct"] == pytest.approx(2.90, abs=0.01)
    assert out["contrib_pct"] == pytest.approx(0.96, abs=0.01)
    assert out["success"] is True
    assert out["fly_pct"] is None


def test_short_not_bought_back_counts_as_fly():
    """倒 T 没接回：卖 10.30、期末 11.00 → 未完成、卖飞、贡献为负。"""
    bars = [
        (date(2026, 8, 3), 10.0, 0.0),
        (date(2026, 8, 4), 10.0, 0.0),
        (date(2026, 8, 5), 11.0, 10.0),
    ]
    out = _metrics(
        _trade(exit_date=None),
        [_t(direction="short", buy_price=None, sell_price=10.3)],
        bars,
        date(2026, 8, 5),
    )[0]
    assert out["completed"] is False
    assert out["success"] is False
    assert out["fly_pct"] == pytest.approx(6.80, abs=0.01)
    assert out["contrib_pct"] < 0
    # 未完成的不进成功率分母
    assert _t_group_stats([out])["success_rate"] is None
    assert _t_group_stats([out])["fly_count"] == 1
    assert _t_group_stats([out])["open"] == 1


def test_long_not_sold_takes_end_price():
    """正 T 没卖出：缺的一腿按期末价计，算未完成但仍有贡献。"""
    bars = [
        (date(2026, 8, 3), 10.0, 0.0),
        (date(2026, 8, 4), 10.0, 0.0),
        (date(2026, 8, 5), 10.5, 5.0),
    ]
    out = _metrics(
        _trade(exit_date=None),
        [_t(buy_price=10.0)],
        bars,
        date(2026, 8, 5),
    )[0]
    assert out["completed"] is False
    assert out["net_pct"] is None  # 扣费后价差只对已完成的有意义
    assert out["spread_pct"] == pytest.approx(5.0, abs=0.01)
    assert out["contrib_pct"] == pytest.approx(1.62, abs=0.01)
    assert out["end_price"] == 10.5


def test_split_between_two_legs_is_not_a_crash():
    """10 送 10 落在两腿之间：50 卖价换算到买入日口径后是 +10%，不是 −45%。"""
    bars = [
        (date(2026, 7, 31), 100.0, 0.0),
        (date(2026, 8, 3), 100.0, 0.0),
        (date(2026, 8, 4), 50.0, 0.0),
        (date(2026, 8, 5), 55.0, 10.0),
    ]
    out = _metrics(
        _trade(entry_date=date(2026, 7, 31), entry_price=100.0, exit_date=None),
        [
            _t(
                trade_date=date(2026, 8, 3),
                leg2_date=date(2026, 8, 5),
                buy_price=100.0,
                sell_price=55.0,
            )
        ],
        bars,
        date(2026, 8, 5),
    )[0]
    assert out["spread_pct"] == pytest.approx(10.0, abs=0.01)
    assert out["contrib_pct"] == pytest.approx(3.27, abs=0.02)


# ---------------------------------------------------------------- 校验（走真库）


@pytest.fixture()
def session():
    """内存库：`create_all` + 打开外键，与线上 `db.py` 的 PRAGMA 一致。"""
    engine = create_engine("sqlite://", future=True)

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(AppUser(username="tester", password_hash="x"))
        for day in DAYS:
            db.add(TradeCalendar(trade_date=day))
        db.commit()
        yield db


def _saved_trade(db: Session, **overrides) -> TradeJournal:
    trade = _trade(**overrides)
    trade.user_id = 1
    db.add(trade)
    db.commit()
    db.refresh(trade)
    return trade


def _payload(**overrides) -> TradeTIn:
    values = {
        "trade_date": date(2026, 8, 4),
        "leg2_date": date(2026, 8, 4),
        "direction": "long",
        "buy_price": 10.0,
        "sell_price": 10.3,
        "ratio_pct": 33.0,
        "trigger": "other",
    }
    values.update(overrides)
    return TradeTIn(**values)


def test_same_day_as_entry_is_rejected(session):
    trade = _saved_trade(session)
    with pytest.raises(HTTPException) as exc:
        _apply_t(session, trade, TradeT(), _payload(trade_date=ENTRY, leg2_date=ENTRY))
    assert exc.value.status_code == 400


def test_closed_position_cannot_have_open_t(session):
    trade = _saved_trade(
        session, exit_date=date(2026, 8, 7), exit_price=11.0
    )
    with pytest.raises(HTTPException) as exc:
        _apply_t(
            session,
            trade,
            TradeT(),
            _payload(leg2_date=None, sell_price=None),
        )
    assert exc.value.status_code == 400
    assert "补上第二腿" in exc.value.detail


def test_move_exit_date_before_t_is_rejected(session):
    trade = _saved_trade(session)
    row = TradeT(
        user_id=1,
        trade_id=trade.id,
        trade_date=date(2026, 8, 5),
        leg2_date=date(2026, 8, 5),
        direction="long",
        buy_price=10.0,
        sell_price=10.3,
        ratio_pct=33.0,
        trigger="other",
    )
    session.add(row)
    session.commit()

    # 卖出日改到 08-04，已有的 08-05 做 T 越界
    with pytest.raises(HTTPException) as exc:
        _check_t_conflicts(session, trade.id, ENTRY, date(2026, 8, 4))
    assert exc.value.status_code == 400
    assert f"T#{row.id}" in exc.value.detail


def test_delete_trade_cascades_to_t(session):
    trade = _saved_trade(session)
    session.add(
        TradeT(
            user_id=1,
            trade_id=trade.id,
            trade_date=date(2026, 8, 4),
            leg2_date=date(2026, 8, 4),
            direction="long",
            buy_price=10.0,
            sell_price=10.3,
            ratio_pct=33.0,
            trigger="other",
        )
    )
    session.commit()
    assert session.scalar(select(func.count()).select_from(TradeT)) == 1

    session.delete(trade)
    session.commit()
    assert session.scalar(select(func.count()).select_from(TradeT)) == 0


def test_price_outside_day_range_only_warns(session):
    """价格不在当天 [low, high] 内：保存成功，指标里带 warnings。"""
    trade = _saved_trade(session)
    row = TradeT()
    _apply_t(session, trade, row, _payload())
    session.add(row)
    session.commit()

    bars = [
        (date(2026, 8, 3), 10.0, 0.0),
        (date(2026, 8, 4), 10.0, 0.0),
    ]
    ranges = {(trade.code, date(2026, 8, 4)): (12.5, 13.1)}
    out = _metrics(
        trade,
        [row],
        bars,
        date(2026, 8, 4),
        ranges,
    )[0]
    assert out["warnings"]
    assert "买入价 10.00" in out["warnings"][0]
