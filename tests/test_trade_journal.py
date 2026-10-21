"""交易日志的指标口径（纯函数）+ 股数校验（内存库）。"""

from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.api.journal import _apply, suggest_reason
from app.models import AppUser, Base, StockDaily, TradeCalendar, TradeJournal
from app.schemas import TradeJournalIn
from app.services.trade_journal import _metrics, load_enriched, reason_label


def _trade(**overrides) -> TradeJournal:
    values = {
        "user_id": 1,
        "code": "600519",
        "entry_date": date(2026, 8, 3),
        "entry_price": 100.0,
        "exit_date": date(2026, 8, 14),
        "exit_price": 110.0,
        "plan_stop": 95.0,
        "reason_type": "pattern",
        "reason_key": "ma_bull",
    }
    values.update(overrides)
    return TradeJournal(**values)


CALENDAR = {
    date(2026, 8, 3): 0,
    date(2026, 8, 4): 1,
    date(2026, 8, 5): 2,
    date(2026, 8, 6): 3,
    date(2026, 8, 7): 4,
    date(2026, 8, 10): 5,
    date(2026, 8, 11): 6,
    date(2026, 8, 12): 7,
    date(2026, 8, 13): 8,
    date(2026, 8, 14): 9,
}


def test_closed_trade_metrics():
    bars = [
        (date(2026, 8, 3), 100.0, 0.0),
        (date(2026, 8, 6), 96.0, -4.0),
        (date(2026, 8, 14), 108.0, 12.5),
    ]
    out = _metrics(_trade(), bars, CALENDAR, date(2026, 8, 14))
    assert out["open"] is False
    assert out["holding_days"] == 10  # 8-03 ~ 8-14，含两端
    assert out["return_pct"] == 10.0  # 用卖出价 110，不用最后收盘 108
    assert out["max_drawdown_pct"] == -4.0
    assert out["stop_hit"] is False
    assert out["stop_executed"] is None
    assert out["metrics_note"] is None


def test_stop_hit_and_executed():
    bars = [
        (date(2026, 8, 3), 100.0, 0.0),
        (date(2026, 8, 6), 90.0, -10.0),
        (date(2026, 8, 14), 108.0, 20.0),
    ]
    out = _metrics(
        _trade(exit_price=94.0), bars, CALENDAR, date(2026, 8, 14)
    )
    assert out["stop_hit"] is True
    assert out["stop_executed"] is True  # 卖出价 94 ≤ 止损位 95
    assert out["return_pct"] == -6.0


def test_stop_hit_but_not_executed():
    bars = [
        (date(2026, 8, 3), 100.0, 0.0),
        (date(2026, 8, 6), 90.0, -10.0),
        (date(2026, 8, 14), 108.0, 20.0),
    ]
    out = _metrics(_trade(exit_price=100.0), bars, CALENDAR, date(2026, 8, 14))
    assert out["stop_hit"] is True
    assert out["stop_executed"] is False  # 跌破后没有在止损位卖出


def test_open_trade_uses_latest_close():
    bars = [
        (date(2026, 8, 3), 100.0, 0.0),
        (date(2026, 8, 14), 108.0, 8.0),
    ]
    out = _metrics(
        _trade(exit_date=None, exit_price=None), bars, CALENDAR, date(2026, 8, 14)
    )
    assert out["open"] is True
    assert out["holding_days"] == 10
    assert out["return_pct"] == 8.0  # 浮动盈亏用最新收盘
    assert out["latest_date"] == date(2026, 8, 14)
    assert out["latest_close"] == 108.0  # 持仓行显示的最新价（不复权）


def test_split_does_not_look_like_a_crash():
    """10 送 10：不复权收盘 100 → 50，但 pct_chg = 0，收益与回撤都应为 0。"""
    bars = [
        (date(2026, 8, 3), 100.0, 0.0),
        (date(2026, 8, 6), 50.0, 0.0),
    ]
    out = _metrics(
        _trade(exit_date=date(2026, 8, 6), exit_price=50.0),
        bars,
        CALENDAR,
        date(2026, 8, 6),
    )
    assert out["return_pct"] == 0.0
    assert out["max_drawdown_pct"] == 0.0
    assert out["stop_hit"] is False
    assert out["metrics_note"] is None


def test_split_with_real_gain():
    """除权日又涨了 10%：raw 50 → 55，pct_chg = +10%，收益应为 +10%。"""
    bars = [
        (date(2026, 8, 3), 100.0, 0.0),
        (date(2026, 8, 6), 55.0, 10.0),
    ]
    out = _metrics(
        _trade(exit_date=date(2026, 8, 6), exit_price=55.0),
        bars,
        CALENDAR,
        date(2026, 8, 6),
    )
    assert out["return_pct"] == 10.0
    assert out["max_drawdown_pct"] == 0.0


def test_missing_entry_bar_reports_note():
    out = _metrics(_trade(), [], CALENDAR, date(2026, 8, 14))
    assert out["return_pct"] is None
    assert out["metrics_note"]


def test_reason_label():
    assert reason_label("pattern", "ma_bull", None) == "均线多头排列"
    assert reason_label("theme", "固态电池", None) == "固态电池"
    assert reason_label("other", None, "朋友推荐") == "朋友推荐"
    # 「其他」里只存了 key 的老记录也要显示出来（2026-10-21 清单 #2）
    assert reason_label("other", "朋友推荐", None) == "朋友推荐"
    assert reason_label("other", None, None) == "其他"


# ------------------------------------------------- 股数 / 买入金额（走真库）


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", future=True)

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(AppUser(username="tester", password_hash="x"))
        for day in (date(2026, 8, 3), date(2026, 8, 4)):
            session.add(TradeCalendar(trade_date=day))
        session.commit()
        yield session


def _payload(**overrides) -> TradeJournalIn:
    values = {
        "code": "600519",
        "entry_date": date(2026, 8, 3),
        "entry_price": 10.0,
        "reason_type": "other",
    }
    values.update(overrides)
    return TradeJournalIn(**values)


def test_entry_amount_from_shares(db):
    """买入金额 = 买入价 × 股数，由后端现算（不落库）。"""
    trade = TradeJournal(user_id=1)
    _apply(db, trade, _payload(shares=1200))
    db.add(trade)
    db.commit()

    row = load_enriched(db, 1)[0]
    assert row["shares"] == 1200
    assert row["entry_amount"] == pytest.approx(12000.0)


def test_shares_must_be_positive(db):
    with pytest.raises(HTTPException) as exc:
        _apply(db, TradeJournal(user_id=1), _payload(shares=0))
    assert exc.value.status_code == 400
    assert "股数" in exc.value.detail


def test_suggest_oversold_date_uses_own_last_bar(db, monkeypatch):
    """乖离的日期取这只票自己的最后一根日线，不是全市场最新那天（复查 P2-1）。"""
    db.add_all(
        [
            StockDaily(code="600519", trade_date=date(2026, 8, 3), close=10.0),
            # 另一只票的日线更新，不能拿它的日期当成 600519 的
            StockDaily(code="600000", trade_date=date(2026, 8, 4), close=10.0),
        ]
    )
    db.commit()
    monkeypatch.setattr(
        "app.api.journal.load_oversold", lambda session, codes: {"600519": -18.0}
    )

    out = suggest_reason("600519", date(2026, 8, 3), session=db, user=None)
    assert out.oversold_dev == -18.0
    assert out.oversold_date == date(2026, 8, 3)
