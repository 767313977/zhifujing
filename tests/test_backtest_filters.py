"""回测脚本的过滤条件框架：只测纯函数（不跑全量回测）。"""

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import backtest_patterns as bt  # noqa: E402

from app.services.patterns import Bars  # noqa: E402


def _bars(closes: list[float]) -> Bars:
    values = np.array(closes, dtype=float)
    return Bars(
        dates=[date(2026, 1, 1) + timedelta(days=index) for index in range(len(values))],
        open=values.copy(),
        high=values.copy(),
        low=values.copy(),
        close=values,
        volume=np.ones_like(values),
        amount=np.ones_like(values),
        pct_chg=np.zeros_like(values),
    )


def test_ma60_up_needs_enough_history():
    bars = _bars([100.0] * 66)
    assert bt.FILTER_CHECKS["ma60_up"]("600000", bars, 63, {}) is False
    # i=64 时两段窗口刚好够：60 根 MA 与「5 日前」那一段都不越界
    assert isinstance(bt.FILTER_CHECKS["ma60_up"]("600000", bars, 64, {}), bool)


def test_ma60_up_uptrend_passes():
    bars = _bars([100.0 + index for index in range(100)])
    assert bt.FILTER_CHECKS["ma60_up"]("600000", bars, 99, {}) is True


def test_ma60_up_downtrend_fails():
    bars = _bars([200.0 - index for index in range(100)])
    assert bt.FILTER_CHECKS["ma60_up"]("600000", bars, 99, {}) is False


def test_ma60_up_flat_fails():
    """横盘时收盘恰好等于 MA60，不满足「收盘 > MA60」。"""
    bars = _bars([100.0] * 100)
    assert bt.FILTER_CHECKS["ma60_up"]("600000", bars, 99, {}) is False


def test_rps_percentile_ranks_three_stocks():
    """三只票 50 日涨幅 −10% / 0% / +10% → RPS 0 / 50 / 100。"""
    flat = [100.0] * 10
    bars = {
        "a": _bars(flat + [90.0] * 50),
        "b": _bars(flat + [100.0] * 50),
        "c": _bars(flat + [110.0] * 50),
    }
    tables = bt._rps_context(bars, {50})
    day = bars["a"].dates[-1]
    assert tables[50][day]["a"] == 0.0
    assert tables[50][day]["b"] == 50.0
    assert tables[50][day]["c"] == 100.0


def test_rps_percentile_skips_short_history():
    """K 线不足 N 根时不参与排名。"""
    bars = {"a": _bars([100.0] * 50)}
    tables = bt._rps_context(bars, {50})
    assert tables[50] == {}


def test_rps50_filter_uses_ctx_table():
    bars = _bars([100.0] * 60)
    day = bars.dates[-1]
    ctx = {"rps": {50: {day: {"600000": 95.0, "600001": 89.9}}}}
    assert bt.FILTER_CHECKS["rps50_90"]("600000", bars, 59, ctx) is True
    assert bt.FILTER_CHECKS["rps50_90"]("600001", bars, 59, ctx) is False
    # 当天没有该票的排名（停牌）→ 不算满足
    assert bt.FILTER_CHECKS["rps50_90"]("600002", bars, 59, ctx) is False


def test_rps_dual_requires_both_windows():
    bars = _bars([100.0] * 60)
    day = bars.dates[-1]
    ctx = {"rps": {50: {day: {"600000": 95.0}}, 120: {day: {"600000": 80.0}}}}
    assert bt.FILTER_CHECKS["rps_dual"]("600000", bars, 59, ctx) is False
    ctx["rps"][120][day]["600000"] = 85.0
    assert bt.FILTER_CHECKS["rps_dual"]("600000", bars, 59, ctx) is True
