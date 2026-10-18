"""次日计划的触发 / 破位判定（纯函数，不碰数据库）。"""

from datetime import date

from app.models import TradePlan
from app.services.trade_plan import _next_returns, evaluate

CALENDAR = [
    date(2026, 10, 9),
    date(2026, 10, 12),
    date(2026, 10, 13),
    date(2026, 10, 14),
]
POS = {day: index for index, day in enumerate(CALENDAR)}


def _plan(buy: float, stop: float) -> TradePlan:
    return TradePlan(
        user_id=1,
        code="600519",
        name="贵州茅台",
        plan_date=date(2026, 10, 9),
        buy_price=buy,
        stop_price=stop,
    )


def test_trigger_and_next_returns():
    bars = {
        date(2026, 10, 9): (110.0, 99.0, 105.0),
        date(2026, 10, 12): (112.0, 104.0, 110.0),
        date(2026, 10, 13): (115.0, 109.0, 114.0),
        date(2026, 10, 14): (118.0, 112.0, 117.0),
    }
    out = evaluate(_plan(100.0, 95.0), bars, CALENDAR, POS, date(2026, 10, 14))
    assert out["status"] == "触发"
    assert out["day_close_pct"] == 5.0  # 105 / 100 - 1
    assert out["max_favorable_pct"] == 10.0  # 最高 110
    assert out["next_1d_pct"] == 10.0  # 10-12 收盘 110
    assert out["next_3d_pct"] == 17.0  # 10-14 收盘 117


def test_broken_without_trigger():
    bars = {date(2026, 10, 9): (101.0, 99.0, 100.0)}
    out = evaluate(_plan(110.0, 100.0), bars, CALENDAR, POS, date(2026, 10, 9))
    assert out["status"] == "放弃（破位）"
    assert out["day_close_pct"] is None


def test_same_day_trigger_and_break():
    bars = {date(2026, 10, 9): (110.0, 90.0, 100.0)}
    out = evaluate(_plan(105.0, 95.0), bars, CALENDAR, POS, date(2026, 10, 9))
    assert out["status"] == "同日触发并跌破"


def test_not_triggered():
    bars = {date(2026, 10, 9): (104.0, 99.0, 100.0)}
    out = evaluate(_plan(105.0, 95.0), bars, CALENDAR, POS, date(2026, 10, 9))
    assert out["status"] == "未触发"


def test_waiting_for_data():
    out = evaluate(_plan(105.0, 95.0), {}, CALENDAR, POS, date(2026, 10, 8))
    assert out["status"] == "待验证"
    assert out["day_close_pct"] is None


def test_next_returns_stop_at_calendar_end():
    bars = {date(2026, 10, 9): (110.0, 99.0, 105.0)}
    out = _next_returns(100.0, bars, CALENDAR, POS, date(2026, 10, 9))
    assert out["next_1d_pct"] is None  # 10-12 没有 bar
    assert out["next_3d_pct"] is None
    assert out["next_5d_pct"] is None
