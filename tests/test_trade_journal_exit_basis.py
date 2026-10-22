"""交易日志「卖出日没有日线」时的收益口径（2026-10-22 修 #2 的钉子）。

旧实现：卖出日查不到 `stock_daily` 行时，`_end_basis` 直接返回最后可用交易日的
等价收盘 —— 等于把用户填的卖出价丢掉、收益按昨收算，而 `metrics_note` 还是空的。
现在的口径（见 `services/trade_journal._end_basis`）：

- 有 `exit_price` + 窗口内**无除权**：直接 `卖出价 / 买入价`，note 写明是直接计算；
- 有 `exit_price` + 窗口内**有除权**：换算不了 → 收益 None + note「暂不计算」；
- 没有 `exit_price`（未平仓）：仍用最新收盘做浮动盈亏（原口径不变）；
- 卖出日**有**日线：仍用 `卖出价 / 当日收盘` 换算（原口径不变）。
"""

from datetime import date

from app.models import TradeJournal
from app.services.trade_journal import _metrics

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


def _trade(**overrides) -> TradeJournal:
    values = {
        "user_id": 1,
        "code": "600519",
        "entry_date": date(2026, 8, 3),
        "entry_price": 100.0,
        "exit_date": date(2026, 8, 14),
        "exit_price": 110.0,
        "plan_stop": None,
        "reason_type": "other",
    }
    values.update(overrides)
    return TradeJournal(**values)


def test_missing_exit_bar_with_exit_price_uses_exit_price_directly():
    """卖出日无日线 + 有卖出价 + 无除权：收益必须按卖出价算，不能拿昨收冒充。"""
    bars = [
        (date(2026, 8, 3), 100.0, 0.0),
        (date(2026, 8, 13), 105.0, 5.0),  # 卖出日 8-14 没有这行
    ]
    out = _metrics(_trade(), bars, CALENDAR, date(2026, 8, 14))
    assert out["return_pct"] == 10.0  # 110 / 100 - 1，而不是 105 / 100 - 1
    assert out["metrics_note"]
    assert "直接计算" in out["metrics_note"]
    assert "无除权" in out["metrics_note"]
    assert out["latest_date"] == date(2026, 8, 13)


def test_missing_exit_bar_without_exit_price_uses_latest_close():
    """卖出日无日线 + 没有卖出价（未平仓）：仍用最新收盘做浮动盈亏，note 留空。"""
    bars = [
        (date(2026, 8, 3), 100.0, 0.0),
        (date(2026, 8, 13), 105.0, 5.0),
    ]
    out = _metrics(
        _trade(exit_date=None, exit_price=None),
        bars,
        CALENDAR,
        date(2026, 8, 13),
    )
    assert out["open"] is True
    assert out["return_pct"] == 5.0
    assert out["metrics_note"] is None


def test_missing_exit_bar_with_split_does_not_guess():
    """卖出日无日线 + 窗口内 10 送 10：不复权卖出价与买入价不可比，收益留 None 并说明。"""
    bars = [
        (date(2026, 8, 3), 100.0, 0.0),
        (date(2026, 8, 6), 50.0, 0.0),  # 10 送 10，真实收益 0
    ]
    out = _metrics(_trade(exit_price=55.0), bars, CALENDAR, date(2026, 8, 14))
    assert out["return_pct"] is None
    assert out["metrics_note"]
    assert "暂不计算" in out["metrics_note"]


def test_exit_bar_present_still_converts_by_close():
    """卖出日有日线时口径不变：卖出价先按当日收盘换算到买入日口径。"""
    bars = [
        (date(2026, 8, 3), 100.0, 0.0),
        (date(2026, 8, 14), 108.0, 8.0),
    ]
    out = _metrics(_trade(), bars, CALENDAR, date(2026, 8, 14))
    assert out["return_pct"] == 10.0  # 108 * (110 / 108) / 100 - 1
    assert out["metrics_note"] is None
