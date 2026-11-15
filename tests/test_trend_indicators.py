"""Trend Engine M1 指标层单测：口径、缺失语义、与既有实现的交叉一致性。"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from app.services import rps
from app.services.patterns import Bars
from app.services.trend_engine import config, indicators, momentum, trend_structure, volume_price


def make_bars(
    closes: list[float],
    *,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    opens: list[float] | None = None,
    volume: list[float] | None = None,
    amount: list[float] | None = None,
    start: date = date(2025, 1, 1),
) -> Bars:
    size = len(closes)
    close = np.asarray(closes, dtype=float)
    high = np.asarray(highs if highs is not None else [c * 1.01 for c in closes], dtype=float)
    low = np.asarray(lows if lows is not None else [c * 0.99 for c in closes], dtype=float)
    open_ = np.asarray(opens if opens is not None else closes, dtype=float)
    vol = np.asarray(volume if volume is not None else [1e6] * size, dtype=float)
    amt = np.asarray(amount if amount is not None else [1e8] * size, dtype=float)
    pct = np.zeros(size)
    if size > 1:
        pct[1:] = (close[1:] / close[:-1] - 1) * 100
    return Bars(
        dates=[start + timedelta(days=index) for index in range(size)],
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=vol,
        amount=amt,
        pct_chg=pct,
        code="000001",
    )


def test_rolling_mean_matches_numpy_and_keeps_nan_prefix() -> None:
    values = np.arange(1, 11, dtype=float)
    ma = indicators.rolling_mean(values, 3)
    assert np.isnan(ma[:2]).all()
    assert ma[2] == pytest.approx(2.0)
    assert ma[-1] == pytest.approx(9.0)
    assert indicators.rolling_mean(values, 11)[-1] != indicators.rolling_mean(values, 11)[-1]


def test_ma_and_slope_are_none_when_history_short() -> None:
    assert indicators.ma_last([1, 2, 3], 5) is None
    assert indicators.slope_pct([1, 2, 3], 5, 5) is None


def test_slope_is_relative_not_absolute() -> None:
    base = [10.0] * 20 + [11.0] * 5
    ten_x = [value * 10 for value in base]
    left = indicators.slope_pct(base, 10, 5)
    right = indicators.slope_pct(ten_x, 10, 5)
    assert left is not None and right is not None
    assert left == pytest.approx(right)


def test_atr_uses_true_range_and_is_none_before_window() -> None:
    close = [10.0] * 20
    high = [10.5] * 20
    low = [9.5] * 20
    array = indicators.atr(high, low, close, 14)
    assert np.isnan(array[12])
    assert array[-1] == pytest.approx(1.0)
    assert indicators.atr_pct(high, low, close, 14) == pytest.approx(10.0)


def test_max_drawdown_and_off_high() -> None:
    values = [10.0, 12.0, 9.0, 11.0]
    assert indicators.max_drawdown_pct(values, 10) == pytest.approx(-25.0)
    assert indicators.off_high_pct(values, 10) == pytest.approx(-8.33, abs=0.01)
    assert indicators.off_high_pct([10.0], 10) is None


def test_off_high_close_and_high_are_distinct_but_shared() -> None:
    close = [10.0, 11.0, 10.5]
    high = [10.2, 12.0, 10.7]
    assert indicators.off_high_close_pct(close, 3) == pytest.approx((10.5 / 11 - 1) * 100, abs=0.01)
    assert indicators.off_high_high_pct(high, close, 3) == pytest.approx((10.5 / 12 - 1) * 100, abs=0.01)
    # 两个函数语义不同，但都必须基于前复权序列、都不允许 0 填充
    assert indicators.off_high_high_pct(high[:1], close[:1], 3) is None


def test_volume_ratio_excludes_today() -> None:
    volume = [100.0] * 5 + [500.0]
    assert indicators.volume_ratio(volume, 5) == pytest.approx(5.0)
    # 当日成交量再翻倍，分母不变
    assert indicators.volume_ratio([100.0] * 5 + [1000.0], 5) == pytest.approx(10.0)
    assert indicators.volume_ratio([0.0] * 6, 5) is None


def test_ret_pct_matches_rps_ret() -> None:
    pct = [1.0, -0.5, 2.0, 0.0, 1.5, -1.0]
    assert indicators.ret_pct(pct, 5) == rps.ret(pct, 5)
    assert indicators.ret_pct(pct, 60) is None


def test_trend_days_counts_consecutive_above_ma20() -> None:
    closes = [10.0] * 25 + [11.0] * 5
    days = indicators.trend_days_above(closes, 20)
    assert days >= 1
    falling = [11.0] * 25 + [9.0] * 5
    assert indicators.trend_days_above(falling, 20) == 0


def test_alignment_and_convergence() -> None:
    rising = [10.0 * (1.01**index) for index in range(80)]
    assert indicators.ma_alignment(rising, (5, 10, 20, 60)) == "bull"
    falling = [10.0 * (0.99**index) for index in range(80)]
    assert indicators.ma_alignment(falling, (5, 10, 20, 60)) == "bear"
    flat = [10.0] * 80
    assert indicators.convergence_pct(flat, (5, 10, 20, 60)) == pytest.approx(0.0)
    assert indicators.ma_alignment([10.0] * 10, (5, 10, 20, 60)) == "insufficient"


def test_quality_flags_missing_bars_and_suspended_day() -> None:
    bars = make_bars([10.0] * 10)
    short = indicators.evaluate_quality(bars, bars.dates[-1], 130)
    assert short.ok is False
    assert any("不足" in item for item in short.missing)

    bars = make_bars([10.0] * 140)
    stale = indicators.evaluate_quality(bars, bars.dates[-1] + timedelta(days=1), 130)
    assert stale.ok is False
    assert any("不是目标日" in item for item in stale.missing)

    fresh = indicators.evaluate_quality(bars, bars.dates[-1], 130)
    assert fresh.ok is True


def test_structure_returns_none_instead_of_zero_when_short() -> None:
    bars = make_bars([10.0] * 30)
    structure = trend_structure.evaluate(bars)
    assert structure.ma[60] is None
    assert structure.distance_ma60_pct is None
    assert "MA60" in structure.missing
    assert structure.trend_days == 0


def test_structure_bull_fields() -> None:
    bars = make_bars([10.0 * (1.01**index) for index in range(160)])
    structure = trend_structure.evaluate(bars)
    assert structure.alignment == "bull"
    assert structure.ma20_turn == "up"
    assert structure.above_ma20 is True
    assert structure.gap_ma20_ma60_pct is not None and structure.gap_ma20_ma60_pct > 0


def test_momentum_rps_and_improvement() -> None:
    bars = make_bars([10.0] * 80)
    history = [
        (bars.dates[-6], {"rps20": 50.0, "rps60": 70.0}),
        (bars.dates[-5], {"rps20": 55.0, "rps60": 71.0}),
        (bars.dates[-4], {"rps20": 60.0, "rps60": 72.0}),
        (bars.dates[-3], {"rps20": 66.0, "rps60": 73.0}),
        (bars.dates[-2], {"rps20": 72.0, "rps60": 74.0}),
        (bars.dates[-1], {"rps20": 80.0, "rps60": 75.0}),
    ]
    state = momentum.evaluate(
        bars,
        {"rps20": 80.0, "rps60": 75.0, "rps120": None, "rps250": 70.0},
        rps_history=history,
        improve_days=5,
    )
    assert state.rps20 == 80.0
    assert state.rps20_improve == pytest.approx(30.0)
    assert state.rps20_consecutive_up == 5
    assert "RPS120" in state.missing
    assert "RPS60" not in state.missing


def test_volume_price_flags_abnormal_and_divergence() -> None:
    closes = [10.0] * 20
    amount = [1e8] * 20 + [9e8]
    bars = make_bars(closes + [10.0], amount=amount)
    state = volume_price.evaluate(bars, config.ModelAConfig())
    assert state.amount_ratio20 is not None and state.amount_ratio20 > 8
    assert state.abnormal_amount is True
    assert state.divergence is None  # 价格为平，不构成背离


def test_effective_weights_sum_to_100_and_respect_bounds() -> None:
    weights = config.effective_weights((("a", 40.0), ("b", 60.0)), 3.0, 0.0)
    assert sum(weights.values()) == pytest.approx(100.0)
    assert weights["rps"] == 3.0
    assert weights["sector"] == 0.0
    assert weights["a"] == pytest.approx(38.8)
    with pytest.raises(ValueError):
        config.effective_weights((("a", 100.0),), 6.0, 0.0)
    with pytest.raises(ValueError):
        config.effective_weights((("a", 100.0),), 3.0, -1.0)


def test_gate_and_band_scores_are_bounded() -> None:
    from app.services.trend_engine import scoring

    assert scoring.gate_score(None, 0, 1) == 0.0
    assert scoring.gate_score(1.5, 1.5, 3.0) == 0.0
    assert scoring.gate_score(3.0, 1.5, 3.0) == 1.0
    assert scoring.gate_score(10.0, 1.5, 3.0) == 1.0
    assert scoring.band_score(2.0, 1.0, 3.0) == 1.0
    assert scoring.band_score(0.0, 1.0, 3.0) == pytest.approx(0.3)
    total, missing = scoring.weighted_total({"a": 1.0, "b": None}, {"a": 50.0, "b": 50.0})
    assert total == 50.0
    assert missing == ("b",)
