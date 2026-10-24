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


def _trend_bars(count: int = 300, start: float = 100.0, step: float = 1.0):
    """稳步上行的序列（高 = 收 ×1.01、低 = 收 ×0.99），用来测趋势模板。"""
    closes = np.array([start + step * index for index in range(count)], dtype=float)
    return Bars(
        dates=[date(2026, 1, 1) + timedelta(days=index) for index in range(count)],
        open=closes.copy(),
        high=closes * 1.01,
        low=closes * 0.99,
        close=closes,
        volume=np.ones_like(closes),
        amount=np.ones_like(closes),
        pct_chg=np.zeros_like(closes),
    )


def _trend_ctx(bars, value: float = 85.0):
    return {"rps": {250: {bars.dates[-1]: {"600000": value}}}}


def test_trend_template_needs_52_weeks():
    """不足 250 根（52 周）一律不通过 —— 短窗口回测只够判最后十来天。"""
    bars = _trend_bars(249)
    assert bt.FILTER_CHECKS["trend_template"]("600000", bars, 248, _trend_ctx(bars)) is False


def test_trend_template_passes_on_steady_uptrend():
    bars = _trend_bars(300)
    assert bt.FILTER_CHECKS["trend_template"]("600000", bars, 299, _trend_ctx(bars)) is True


def test_trend_template_requires_rs_and_rps_table():
    bars = _trend_bars(300)
    # 没有 RPS 表 → 第 8 条判不了，直接不通过（不猜）
    assert bt.FILTER_CHECKS["trend_template"]("600000", bars, 299, {}) is False
    # RS 只有 60 → 不满足 ≥70
    assert (
        bt.FILTER_CHECKS["trend_template"]("600000", bars, 299, _trend_ctx(bars, 60.0))
        is False
    )


def test_trend_template_rejects_flat_market():
    bars = _trend_bars(300, step=0.0)
    assert (
        bt.FILTER_CHECKS["trend_template"]("600000", bars, 299, _trend_ctx(bars)) is False
    )


def _open_bars(opens: list[float], closes: list[float]) -> Bars:
    """开 / 收分开给的序列，用来测入场价口径。"""
    open_ = np.array(opens, dtype=float)
    close = np.array(closes, dtype=float)
    return Bars(
        dates=[date(2026, 1, 1) + timedelta(days=index) for index in range(len(close))],
        open=open_,
        high=np.maximum(open_, close),
        low=np.minimum(open_, close),
        close=close,
        volume=np.ones_like(close),
        amount=np.ones_like(close),
        pct_chg=np.zeros_like(close),
    )


def test_entry_close_uses_signal_day_close():
    bars = _open_bars([10.0, 11.0, 12.0], [10.5, 11.5, 12.5])
    assert bt._entry_price(bars, 1, bt.ENTRY_CLOSE) == 11.5


def test_entry_next_open_uses_following_open():
    """次日开盘入场：信号日（i=1）收盘 11.5 买不到，只能 i=2 开盘 12.0 成交。"""
    bars = _open_bars([10.0, 11.0, 12.0], [10.5, 11.5, 12.5])
    assert bt._entry_price(bars, 1, bt.ENTRY_NEXT_OPEN) == 12.0


def test_entry_next_open_returns_none_on_last_bar():
    """最后一根没有「次日」，拿不到开盘价 —— 不能退回收盘价顶上。"""
    bars = _open_bars([10.0, 11.0], [10.5, 11.5])
    assert bt._entry_price(bars, 1, bt.ENTRY_NEXT_OPEN) is None

