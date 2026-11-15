"""M6.1-E 交易级回测单测：T+1、涨跌停、停牌、成本、退出与组合约束。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.trend_engine.trade_sim import DailyBar, Signal, SimConfig, simulate

START = date(2025, 1, 2)
DAYS = [START + timedelta(days=index) for index in range(12)]


def bar(open_: float, close: float, *, pct: float | None = None, amount: float = 1e8) -> DailyBar:
    if pct is None:
        pct = 0.0
    return DailyBar(
        open=open_,
        high=max(open_, close) * 1.01,
        low=min(open_, close) * 0.99,
        close=close,
        pct=pct,
        amount=amount,
        volume=1e6,
    )


def flat_series(price: float = 10.0, days: list[date] | None = None) -> dict[date, DailyBar]:
    days = days or DAYS
    return {day: bar(price, price, pct=0.0) for day in days}


def test_buy_executes_next_open_not_signal_close() -> None:
    bars = {"600001": flat_series()}
    result = simulate(
        bars_by_code=bars,
        calendar=DAYS,
        signals=[Signal("600001", DAYS[2], 1.0)],
        config=SimConfig(horizon=2, slippage_bps=0, commission_bps=0, min_commission=0),
    )
    assert result.trades
    trade = result.trades[0]
    assert trade.entry_date == DAYS[3]
    assert trade.entry_price == pytest.approx(10.0)


def test_horizon_exit_is_next_open() -> None:
    bars = {"600001": flat_series()}
    result = simulate(
        bars_by_code=bars,
        calendar=DAYS,
        signals=[Signal("600001", DAYS[0], 1.0)],
        config=SimConfig(horizon=2, slippage_bps=0, commission_bps=0, min_commission=0),
    )
    trade = result.trades[0]
    assert trade.entry_date == DAYS[1]
    # 第 2 个交易日收盘触发（hold_days=2），下一交易日开盘卖出
    assert trade.exit_date == DAYS[4]
    assert trade.reason == "horizon"


def test_limit_up_blocks_buy() -> None:
    days = DAYS[:6]
    bars = {"600001": {day: bar(10.0, 10.0) for day in days}}
    bars["600001"][days[2]] = bar(10.0, 10.0)
    # 信号日后的开盘一字涨停：前收 10 → 11
    bars["600001"][days[3]] = bar(11.0, 11.0, pct=10.0)
    result = simulate(
        bars_by_code=bars,
        calendar=days,
        signals=[Signal("600001", days[2], 1.0)],
        config=SimConfig(horizon=3),
    )
    assert result.trades == []
    assert result.skipped["limit_up"] >= 1


def test_suspension_delays_entry() -> None:
    days = DAYS[:9]
    bars = {"600001": {day: bar(10.0, 10.0) for day in days if day != days[3]}}
    result = simulate(
        bars_by_code=bars,
        calendar=days,
        signals=[Signal("600001", days[2], 1.0)],
        config=SimConfig(horizon=1),
    )
    assert result.skipped["suspended"] >= 1
    assert result.trades
    assert result.trades[0].entry_date == days[4]


def test_costs_reduce_pnl() -> None:
    bars = {"600001": flat_series()}
    result = simulate(
        bars_by_code=bars,
        calendar=DAYS,
        signals=[Signal("600001", DAYS[0], 1.0)],
        config=SimConfig(horizon=2, slippage_bps=10, commission_bps=3, stamp_bps=10),
    )
    trade = result.trades[0]
    assert trade.pnl < 0  # 平价买卖也应因成本亏损


def test_max_positions_and_cash_allocation() -> None:
    codes = ["600001", "600002", "600003"]
    bars = {code: flat_series() for code in codes}
    result = simulate(
        bars_by_code=bars,
        calendar=DAYS,
        signals=[Signal(code, DAYS[0], 1.0) for code in codes],
        config=SimConfig(max_positions=2, position_pct=0.5, horizon=2),
    )
    assert len(result.trades) <= 2
    assert result.skipped["slots"] >= 1


def test_atr_stop_exits_after_close_break() -> None:
    days = [START + timedelta(days=index) for index in range(32)]
    prices = [10.0] * 25 + [9.0] * 7
    bars = {
        "600001": {
            day: bar(prices[index], prices[index], pct=(prices[index] / prices[index - 1] - 1) * 100 if index else 0.0)
            for index, day in enumerate(days)
        }
    }
    result = simulate(
        bars_by_code=bars,
        calendar=days,
        signals=[Signal("600001", days[20], 1.0)],
        config=SimConfig(horizon=10, use_atr_stop=True, atr_stop_multiple=0.5),
    )
    assert result.trades
    assert result.trades[0].reason == "atr_stop"


def test_suspended_position_keeps_last_price() -> None:
    days = DAYS[:7]
    bars = {"600001": {day: bar(10.0, 10.0) for day in days if day != days[4]}}
    result = simulate(
        bars_by_code=bars,
        calendar=days,
        signals=[Signal("600001", days[0], 1.0)],
        config=SimConfig(horizon=20),
    )
    values = dict(result.equity)
    # 停牌日不能把持仓市值算成 0（否则权益会突然塌陷）
    assert values[days[4]] > values[days[3]] * 0.9


def test_ma_exit_reason() -> None:
    days = DAYS[:10]
    closes = [10.0] * 4 + [9.0] * 6
    bars = {
        "600001": {
            day: bar(closes[index], closes[index], pct=(closes[index] / closes[index - 1] - 1) * 100 if index else 0.0)
            for index, day in enumerate(days)
        }
    }
    result = simulate(
        bars_by_code=bars,
        calendar=days,
        signals=[Signal("600001", days[0], 1.0)],
        config=SimConfig(horizon=20, use_ma_exit=True, ma_exit_window=20),
    )
    # 只有 10 根 K 线，MA20 不可用 → 不应误触发 ma_break
    assert all(trade.reason != "ma_break" for trade in result.trades)
