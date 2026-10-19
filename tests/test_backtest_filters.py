"""回测脚本的过滤条件框架：只测纯函数（不跑全量回测）。"""

import sys
from datetime import date
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
        dates=[date(2026, 1, 1)] * len(values),
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
