"""M6.3 双价格域与模拟器修复测试（F3/F4/F6/F7 + 复权样本）。"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.services.trend_engine.pricing import (
    PriceDomainError,
    adjusted_series_is_continuous,
    build_price_bar,
    to_execution_bar,
    to_signal_bar,
)
from app.services.trend_engine.trade_sim import DailyBar, Signal, SimConfig, simulate

START = date(2025, 1, 2)
DAYS = [START + timedelta(days=index) for index in range(16)]


def bar(open_: float, close: float, *, pct: float | None = None, amount: float = 1e8) -> DailyBar:
    return DailyBar(
        open=open_,
        high=max(open_, close) * 1.001,
        low=min(open_, close) * 0.999,
        close=close,
        pct=pct if pct is not None else 0.0,
        amount=amount,
        volume=1e6,
    )


def flat(price: float, days: list[date] | None = None) -> dict[date, DailyBar]:
    return {day: bar(price, price) for day in (days or DAYS)}


def config(**kwargs) -> SimConfig:
    return SimConfig(signal_price_mode="RAW_APPROX", **kwargs)


# ---------------------------------------------------------------- 价格域


def test_missing_factor_blocks_signal_bar() -> None:
    price = build_price_bar(
        code="600001",
        trade_date=DAYS[0],
        raw_open=10.0,
        raw_high=10.2,
        raw_low=9.8,
        raw_close=10.0,
    )
    assert price.adjustment_status == "UNKNOWN"
    assert price.adjusted_close is None
    with pytest.raises(PriceDomainError):
        to_signal_bar(price)
    assert to_execution_bar(price).close == Decimal("10.0")


def test_forward_factor_maps_to_adjusted_without_changing_raw() -> None:
    price = build_price_bar(
        code="600001",
        trade_date=DAYS[0],
        raw_open=10.0,
        raw_high=10.2,
        raw_low=9.8,
        raw_close=10.0,
        adj_factor=Decimal("0.5"),
        adjustment_method="FORWARD",
        adjustment_reference_date=DAYS[-1],
    )
    assert price.adjustment_status == "OK"
    signal_bar = to_signal_bar(price)
    assert signal_bar.close == Decimal("5.0")
    assert to_execution_bar(price).close == Decimal("10.0")


def test_cash_dividend_adjusted_series_continuous() -> None:
    # 除息前 raw 10.0、除息日 raw 9.0（派 1 元）。
    # 前复权因子：除息前 0.9、除息日起 1.0 → 调整后 9.0 / 9.0，连续。
    previous = build_price_bar(
        code="600001",
        trade_date=DAYS[0],
        raw_open=10.0,
        raw_high=10.1,
        raw_low=9.9,
        raw_close=10.0,
        adj_factor=Decimal("0.9"),
        adjustment_method="FORWARD",
    )
    current = build_price_bar(
        code="600001",
        trade_date=DAYS[1],
        raw_open=9.0,
        raw_high=9.1,
        raw_low=8.9,
        raw_close=9.0,
        adj_factor=Decimal("1.0"),
        adjustment_method="FORWARD",
    )
    assert adjusted_series_is_continuous(previous.adjusted_close, current.adjusted_close, tolerance_pct=0.5)
    # 不传因子时无法证明连续性
    naive = build_price_bar(
        code="600001",
        trade_date=DAYS[1],
        raw_open=9.0,
        raw_high=9.1,
        raw_low=8.9,
        raw_close=9.0,
    )
    assert naive.adjusted_close is None
    assert not adjusted_series_is_continuous(previous.adjusted_close, naive.adjusted_close)


def test_stock_split_adjusted_continuous() -> None:
    # 2:1 拆股：除权前因子 0.5，除权日起 1.0 → 10×0.5 = 5×1.0
    previous = build_price_bar(
        code="600001",
        trade_date=DAYS[0],
        raw_open=10.0,
        raw_high=10.0,
        raw_low=10.0,
        raw_close=10.0,
        adj_factor=Decimal("0.5"),
        adjustment_method="FORWARD",
    )
    current = build_price_bar(
        code="600001",
        trade_date=DAYS[1],
        raw_open=5.0,
        raw_high=5.0,
        raw_low=5.0,
        raw_close=5.0,
        adj_factor=Decimal("1.0"),
        adjustment_method="FORWARD",
    )
    assert adjusted_series_is_continuous(previous.adjusted_close, current.adjusted_close)


def test_abnormal_factor_rejected_or_flagged() -> None:
    price = build_price_bar(
        code="600001",
        trade_date=DAYS[0],
        raw_open=10.0,
        raw_high=10.0,
        raw_low=10.0,
        raw_close=10.0,
        adj_factor=Decimal("0"),
        adjustment_method="FORWARD",
    )
    assert price.adjustment_status == "INVALID_DATA"
    with pytest.raises(PriceDomainError):
        to_signal_bar(price)
    assert not adjusted_series_is_continuous(Decimal("10"), Decimal("16"), tolerance_pct=50)


def test_invalid_ohlc_rejected() -> None:
    with pytest.raises(PriceDomainError):
        build_price_bar(
            code="600001",
            trade_date=DAYS[0],
            raw_open=10.0,
            raw_high=9.0,
            raw_low=11.0,
            raw_close=10.0,
        )


# ---------------------------------------------------------------- 模拟器


def test_signal_price_mode_must_be_declared() -> None:
    with pytest.raises(PriceDomainError):
        simulate(
            bars_by_code={"600001": flat(10.0)},
            calendar=DAYS,
            signals=[Signal("600001", DAYS[0], 1.0)],
            config=SimConfig(horizon=2),
        )
    with pytest.raises(PriceDomainError):
        simulate(
            bars_by_code={"600001": flat(10.0)},
            calendar=DAYS,
            signals=[Signal("600001", DAYS[0], 1.0)],
            config=SimConfig(horizon=2, signal_price_mode="ADJUSTED"),
        )


def test_adjusted_signal_bars_do_not_change_execution_price() -> None:
    raw = {"600001": flat(10.0)}
    signal = {"600001": flat(20.0)}
    result = simulate(
        bars_by_code=raw,
        calendar=DAYS,
        signals=[Signal("600001", DAYS[0], 1.0)],
        config=SimConfig(horizon=2, signal_price_mode="ADJUSTED", slippage_bps=0, min_commission=0),
        signal_bars=signal,
    )
    assert result.price_domain_quality == "ADJUSTED"
    assert result.trades[0].entry_price == pytest.approx(10.0)


def test_sizing_uses_previous_close_equity_not_today_close() -> None:
    # A 已持仓，DAYS[2] 收盘从 10 跳到 100；B 在 DAYS[2] 开盘买入。
    # 正确：用 DAYS[1] 收盘权益（1,000,000 - A 的买入佣金 30）× 10% ≈ 99,997 → 9,900 股。
    # 错误（旧实现）：用 DAYS[2] 收盘权益 1,899,970 × 10% = 189,997 → 18,900 股。
    bars = {
        "600001": {**flat(10.0), DAYS[2]: bar(10.0, 100.0)},
        "600002": flat(10.0),
    }
    result = simulate(
        bars_by_code=bars,
        calendar=DAYS,
        signals=[Signal("600001", DAYS[0], 2.0), Signal("600002", DAYS[1], 1.0)],
        config=config(horizon=10, max_positions=2, position_pct=0.1, slippage_bps=0, min_commission=0),
    )
    b_trade = [trade for trade in result.trades if trade.code == "600002"]
    assert b_trade, "B 应在 DAYS[2] 买入并在样本内退出"
    assert b_trade[0].shares == 9_900
    assert b_trade[0].entry_date == DAYS[2]


def test_duplicate_signals_are_idempotent() -> None:
    result = simulate(
        bars_by_code={"600001": flat(10.0)},
        calendar=DAYS,
        signals=[
            Signal("600001", DAYS[0], 1.0, "m1"),
            Signal("600001", DAYS[0], 1.0, "m1"),
            Signal("600001", DAYS[0], 1.0, "m1"),
        ],
        config=config(horizon=2),
    )
    assert result.skipped["duplicate"] == 2
    assert any("DUPLICATE_SIGNAL_DROPPED" in item for item in result.warnings)
    assert len([trade for trade in result.trades if trade.code == "600001"]) == 1


def test_trade_records_signal_trigger_and_fill_dates() -> None:
    result = simulate(
        bars_by_code={"600001": flat(10.0)},
        calendar=DAYS,
        signals=[Signal("600001", DAYS[0], 1.0, "m1")],
        config=config(horizon=2, slippage_bps=0, min_commission=0),
    )
    trade = result.trades[0]
    assert trade.signal_date == DAYS[0]
    assert trade.exit_trigger_date is not None
    assert trade.exit_fill_date == trade.exit_date
    assert trade.exit_trigger_date < trade.exit_fill_date
    assert trade.holding_trading_days > 0
    assert trade.model_version == "m1"


def test_unvaluable_position_is_flagged_not_silently_closed() -> None:
    days = DAYS[:12]
    # 买入后长期无行情（模拟停牌/退市）
    bars = {"600001": {day: bar(10.0, 10.0) for day in days[:3]}}
    result = simulate(
        bars_by_code=bars,
        calendar=days,
        signals=[Signal("600001", days[0], 1.0)],
        config=config(horizon=1, max_exit_delay=2),
    )
    assert result.unvaluable
    assert result.unvaluable[0]["reason"] == "SUSPENDED_EXCEEDED_MAX_EXIT_DELAY"
    assert result.metrics["status"] == "PENDING_REVIEW"
    assert result.metrics["unvaluable_positions"] == 1
    assert any("UNVALUABLE_POSITION" in item for item in result.warnings)


def test_randomized_ledger_invariants_with_fixed_seed() -> None:
    import random

    for seed in range(8):
        rng = random.Random(seed)
        days = DAYS[:40]
        codes = [f"60000{index}" for index in range(5)]
        bars: dict[str, dict[date, DailyBar]] = {}
        for code in codes:
            series = {}
            price = 10.0 + rng.random() * 5
            for day in days:
                previous = price
                price = max(1.0, previous * (1 + rng.uniform(-0.05, 0.05)))
                series[day] = bar(previous, price, pct=(price / previous - 1) * 100, amount=1e8 * (0.5 + rng.random()))
            bars[code] = series
        signals = [
            Signal(rng.choice(codes), rng.choice(days[:20]), rng.random(), "m1")
            for _ in range(30)
        ]
        result = simulate(
            bars_by_code=bars,
            calendar=days,
            signals=signals,
            config=config(horizon=5, max_positions=3, position_pct=0.25),
        )
        for _day, cash, value, equity in result.ledger:
            assert cash >= -1e-6
            assert abs(cash + value - equity) < 0.02
        assert all(trade.shares % 100 == 0 for trade in result.trades)
        assert all(trade.exit_date > trade.entry_date for trade in result.trades)
        pnl = sum(trade.pnl_exact for trade in result.trades)
        unrealized = sum(item["unrealized_pnl_exact"] for item in result.open_positions)
        equity_change = result.ledger[-1][3] - 1_000_000.0
        assert abs(equity_change - pnl - unrealized) < 0.05
