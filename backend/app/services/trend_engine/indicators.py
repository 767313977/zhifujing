"""Trend Engine 指标层：**纯函数**，不查库、不写库、不读全局状态。

口径约定（与 `services/rps.py`、`services/patterns.py` 对齐）：

- 价格序列一律是 `patterns.Bars` 的**前复权**序列（`build_bars` 由 `pct_chg` 复利得到）；
- 区间收益用 `pct_chg` 复利（`rps.ret`），不用收盘价比值；
- 成交量比 / 成交额比的**分母不含当日**（当日不能出现在自己的基准里）；
- ATR 用真实波幅的简单均值（与 `patterns._low_atr_growth` 一致），不是 Wilder 平滑；
- 数据不足一律返回 `None`，**绝不返回 0**；调用方必须显式处理缺失。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np

from app.services import rps
from app.services.patterns import Bars

_EPS = 1e-12


# ---------------------------------------------------------------- 基础工具


def _arr(values) -> np.ndarray:
    out = np.asarray(values, dtype=float)
    if out.ndim != 1:
        raise ValueError("指标只接受一维序列")
    return out


def _finite_last(values: np.ndarray) -> float | None:
    if values.size == 0:
        return None
    value = float(values[-1])
    return value if np.isfinite(value) else None


def rolling_mean(values, window: int) -> np.ndarray:
    """滚动均值，返回与输入等长的数组；前 `window-1` 个为 NaN。

    窗口内出现非有限值（停牌残行没被剔除时的防御）→ 该点记 NaN，不拿部分窗口凑数。
    """
    if window <= 0:
        raise ValueError("window 必须为正")
    series = _arr(values)
    out = np.full(series.size, np.nan)
    if series.size < window:
        return out
    valid = np.isfinite(series)
    sums = np.convolve(np.where(valid, series, 0.0), np.ones(window), mode="valid")
    counts = np.convolve(valid.astype(int), np.ones(window, dtype=int), mode="valid")
    out[window - 1 :] = np.where(counts == window, sums / window, np.nan)
    return out


def ma_last(values, window: int) -> float | None:
    series = _arr(values)
    if series.size < window:
        return None
    window_values = series[-window:]
    if not np.all(np.isfinite(window_values)):
        return None
    return float(np.mean(window_values))


def slope_pct(values, window: int, days: int = 5) -> float | None:
    """MA(window) 在 `days` 个交易日内的变化率（%）。

    `values` 是价格序列；返回 `None` 表示历史不足。**不用价格绝对差**，所以不同价位的票可比。
    """
    series = _arr(values)
    if days <= 0 or series.size < window + days:
        return None
    now = ma_last(series, window)
    before = ma_last(series[:-days], window)
    if now is None or before is None or abs(before) < _EPS:
        return None
    return round((now / before - 1) * 100, 2)


def normalized_slope(values, window: int, days: int, atr_pct_value: float | None) -> float | None:
    """波动率归一化斜率 = 均线变化率(%) / ATR%。低波动股的高斜率更有信息量。"""
    slope = slope_pct(values, window, days)
    if slope is None or atr_pct_value is None or atr_pct_value <= 0:
        return None
    return round(slope / atr_pct_value, 2)


# ---------------------------------------------------------------- 波动 / 回撤


def true_range(high, low, close) -> np.ndarray:
    high_arr, low_arr, close_arr = _arr(high), _arr(low), _arr(close)
    if not (high_arr.size == low_arr.size == close_arr.size):
        raise ValueError("high/low/close 长度必须一致")
    out = high_arr - low_arr
    if close_arr.size > 1:
        prev = close_arr[:-1]
        out[1:] = np.maximum.reduce(
            [
                high_arr[1:] - low_arr[1:],
                np.abs(high_arr[1:] - prev),
                np.abs(low_arr[1:] - prev),
            ]
        )
    return out


def atr(high, low, close, window: int = 14) -> np.ndarray:
    """ATR（真实波幅的简单均值），返回等长数组，前 `window-1` 个为 NaN。"""
    return rolling_mean(true_range(high, low, close), window)


def atr_last(high, low, close, window: int = 14) -> float | None:
    return _finite_last(atr(high, low, close, window))


def atr_pct(high, low, close, window: int = 14) -> float | None:
    """ATR / 收盘价 × 100（%）。"""
    value = atr_last(high, low, close, window)
    close_value = _finite_last(_arr(close))
    if value is None or close_value is None or close_value <= 0:
        return None
    return round(value / close_value * 100, 2)


def max_drawdown_pct(values, window: int) -> float | None:
    """最近 `window` 个交易日内的最大回撤（%，≤0）。按收盘价峰谷计算。"""
    series = _arr(values)
    if series.size < 2:
        return None
    chunk = series[-window:] if window > 0 else series
    if chunk.size < 2 or not np.all(np.isfinite(chunk)):
        return None
    running_peak = np.maximum.accumulate(chunk)
    drawdown = chunk / running_peak - 1
    return round(float(drawdown.min()) * 100, 2)


def off_high_pct(values, window: int) -> float | None:
    """最新值距最近 `window` 个点最高**收盘**的幅度（%，≤0）。等价于 `off_high_close_pct`。"""
    return off_high_close_pct(values, window)


def off_high_close_pct(close, window: int) -> float | None:
    """收盘距最高**收盘**（%，≤0）。前复权序列上计算，避免除权跳空。"""
    series = _arr(close)
    if series.size < 2:
        return None
    chunk = series[-window:] if window > 0 else series
    if chunk.size < 2 or not np.all(np.isfinite(chunk)):
        return None
    peak = float(np.max(chunk))
    if peak <= 0:
        return None
    return round((float(chunk[-1]) / peak - 1) * 100, 2)


def off_high_high_pct(high, close, window: int) -> float | None:
    """收盘距最高**价**（%，≤0）。

    与 `off_high_close_pct` 是两个不同指标，调用方必须显式选择：
    「距最高收盘」用于净值/回撤类研究，「距最高价」用于压力位/突破类研究。
    """
    high_arr, close_arr = _arr(high), _arr(close)
    if high_arr.size != close_arr.size or high_arr.size < 2:
        return None
    chunk = high_arr[-window:] if window > 0 else high_arr
    if chunk.size < 2 or not np.all(np.isfinite(chunk)):
        return None
    peak = float(np.max(chunk))
    last = float(close_arr[-1])
    if peak <= 0 or not np.isfinite(last):
        return None
    return round((last / peak - 1) * 100, 2)


def rolling_high(values, window: int, *, exclude_last: bool = False) -> float | None:
    """最近 `window` 个点的最高值；`exclude_last=True` 时窗口是 `[-(window+1):-1]`。

    平台高点必须用 `exclude_last=True`：当天的高点不能当自己的突破参考（PIT）。
    """
    series = _arr(values)
    if exclude_last:
        if series.size < window + 1:
            return None
        chunk = series[-window - 1 : -1]
    else:
        if series.size < window:
            return None
        chunk = series[-window:]
    if chunk.size == 0 or not np.all(np.isfinite(chunk)):
        return None
    return float(np.max(chunk))


def rolling_low(values, window: int, *, exclude_last: bool = False) -> float | None:
    series = _arr(values)
    if exclude_last:
        if series.size < window + 1:
            return None
        chunk = series[-window - 1 : -1]
    else:
        if series.size < window:
            return None
        chunk = series[-window:]
    if chunk.size == 0 or not np.all(np.isfinite(chunk)):
        return None
    return float(np.min(chunk))


# ---------------------------------------------------------------- 量能


def volume_ratio(volume, window: int = 5) -> float | None:
    """当日成交量 / 前 `window` 日均量（**分母不含当日**）。"""
    series = _arr(volume)
    if series.size < window + 1:
        return None
    base = series[-window - 1 : -1]
    if not np.all(np.isfinite(base)):
        return None
    mean = float(np.mean(base))
    if mean <= 0:
        return None
    return round(float(series[-1]) / mean, 2)


def amount_ratio(amount, window: int = 20) -> float | None:
    """当日成交额 / 前 `window` 日均额（**分母不含当日**）。单位：元。"""
    return volume_ratio(amount, window)


def up_down_volume_ratio(close, volume, window: int = 20) -> float | None:
    """上涨日成交量之和 / 下跌日成交量之和（最近 `window` 日，含当日）。

    下跌日成交量为 0 时返回 None（不是无穷大）：那种窗口没有可比性。
    """
    close_arr, volume_arr = _arr(close), _arr(volume)
    if close_arr.size < window + 1 or volume_arr.size != close_arr.size:
        return None
    pct = np.diff(close_arr[-(window + 1) :]) / np.where(
        close_arr[-(window + 1) : -1] != 0, close_arr[-(window + 1) : -1], np.nan
    )
    vol = volume_arr[-window:]
    if not np.all(np.isfinite(pct)) or not np.all(np.isfinite(vol)):
        return None
    up = float(np.sum(vol[pct > 0]))
    down = float(np.sum(vol[pct < 0]))
    if down <= 0:
        return None
    return round(up / down, 2)


# ---------------------------------------------------------------- 收益 / 趋势


def ret_pct(pct_chg, window: int) -> float | None:
    """区间涨幅（%），与线上 RPS 同为 `pct_chg` 复利 + 90% 覆盖闸。"""
    series = _arr(pct_chg)
    return rps.ret([float(value) for value in series if np.isfinite(value)], window)


def trend_days_above(close, window: int = 20, *, max_days: int = 250) -> int:
    """从最新一根往前数，连续多少个交易日收盘 > 对应 MA(window)。"""
    close_arr = _arr(close)
    ma = rolling_mean(close_arr, window)
    count = 0
    for index in range(close_arr.size - 1, window - 2, -1):
        if not np.isfinite(ma[index]) or not np.isfinite(close_arr[index]):
            break
        if close_arr[index] > ma[index]:
            count += 1
            if count >= max_days:
                break
        else:
            break
    return count


def ma_alignment(close, periods: tuple[int, ...] = (5, 10, 20, 60)) -> str:
    """均线排列：`bull` / `bear` / `mixed` / `insufficient`。

    `bull` = 短周期均线全部高于长周期（MA5>MA10>MA20>MA60）；
    `bear` = 完全相反。只比较**相邻**周期，避免把 MA5 与 MA60 的顺序当成唯一判据。
    """
    values = [ma_last(close, period) for period in periods]
    if any(value is None for value in values):
        return "insufficient"
    pairs = list(zip(values[:-1], values[1:]))
    if all(left > right for left, right in pairs):
        return "bull"
    if all(left < right for left, right in pairs):
        return "bear"
    return "mixed"


def convergence_pct(close, periods: tuple[int, ...] = (5, 10, 20, 60)) -> float | None:
    """均线收敛度：(最大 MA − 最小 MA) / 平均 MA × 100。越小越收敛。"""
    values = [ma_last(close, period) for period in periods]
    if any(value is None for value in values):
        return None
    mean = float(np.mean(values))
    if mean <= 0:
        return None
    return round((max(values) - min(values)) / mean * 100, 2)


def close_position(high, low, close) -> float | None:
    """当日收盘在振幅中的位置（0~1）。振幅为 0 时返回 None。"""
    high_value = _finite_last(_arr(high))
    low_value = _finite_last(_arr(low))
    close_value = _finite_last(_arr(close))
    if high_value is None or low_value is None or close_value is None:
        return None
    span = high_value - low_value
    if span <= 0:
        return None
    return round((close_value - low_value) / span, 3)


def upper_shadow_pct(open_, high, low, close) -> float | None:
    """上影线占当日振幅的比例（%）。"""
    open_value = _finite_last(_arr(open_))
    high_value = _finite_last(_arr(high))
    low_value = _finite_last(_arr(low))
    close_value = _finite_last(_arr(close))
    if None in (open_value, high_value, low_value, close_value):
        return None
    span = high_value - low_value
    if span <= 0:
        return None
    shadow = high_value - max(open_value, close_value)
    return round(max(shadow, 0.0) / span * 100, 2)


# ---------------------------------------------------------------- 数据质量


@dataclass(frozen=True, slots=True)
class QualityState:
    """一只票在目标日的可用性。`ok=False` 的票**不得进入评分**。"""

    ok: bool
    bars: int
    last_date: date | None
    missing: tuple[str, ...] = field(default_factory=tuple)


def evaluate_quality(bars: Bars | None, day: date | None, min_bars: int) -> QualityState:
    """数据完整性检查：根数、最新日期、价格/成交额是否有限。

    `day` 给定时，最新一根必须就是 `day` —— 否则说明停牌或数据没采到，
    不能拿前几天的 K 线当天用。
    """
    if bars is None or len(bars) == 0:
        return QualityState(False, 0, None, ("没有日线",))
    missing: list[str] = []
    if len(bars) < min_bars:
        missing.append(f"日线不足 {min_bars} 根")
    last_date = bars.dates[-1] if bars.dates else None
    if day is not None and last_date != day:
        missing.append(f"最新日线 {last_date} 不是目标日 {day}")
    close_ok = np.all(np.isfinite(bars.close[-min_bars:])) if len(bars.close) else False
    amount_ok = np.all(np.isfinite(bars.amount[-min_bars:])) if len(bars.amount) else False
    if not close_ok:
        missing.append("收盘价含非有限值")
    if not amount_ok:
        missing.append("成交额含非有限值")
    return QualityState(not missing, len(bars), last_date, tuple(missing))
