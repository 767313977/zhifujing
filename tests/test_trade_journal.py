"""交易日志的指标口径（纯函数，不碰数据库）。"""

from datetime import date

from app.models import TradeJournal
from app.services.trade_journal import _metrics, reason_label


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
    assert reason_label("other", None, None) == "其他"
