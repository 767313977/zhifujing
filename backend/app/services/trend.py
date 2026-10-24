"""Minervini 趋势模板（8 条）—— **页面与回测共用这一份**。

原实现写在 `scripts/backtest_patterns.py` 里（2026-10-24 加的）。选股池也要用同一条
判据，两处各写一份必然漂移（`patterns.is_st` 那次就是教训），所以挪到这里，回测脚本
改成 import。

逐条对应（全部满足才算通过）：

1. 收盘 > MA150 且 > MA200
2. MA150 > MA200
3. MA200 比 20 个交易日前高（至少向上一个月）
4. MA50 > MA150 > MA200
5. 收盘 > MA50
6. 收盘 ≥ 52 周最低 × 1.3
7. 收盘 ≥ 52 周最高 × 0.75
8. RS ≥ 70 —— 用 250 日 RPS 分位（与站内 RPS 同口径），由调用方传进来

⚠️ 52 周按 **250 个交易日**算，所以 `i < 249` 一律不通过；主库只有 260 根日线，
只够判最后十来天，要下结论得用长历史回测。
"""

import numpy as np

from app.services.patterns import Bars

#: 第 8 条的门槛（RS ≥ 70）
TREND_RS_MIN = 70.0

#: 「52 周」按交易日算
TREND_LOOKBACK = 250


def passes(bars: Bars, i: int, rs: float | None) -> bool:
    """第 `i` 根 K 线当天是否满足趋势模板。`rs` = 当天的 250 日 RPS 分位（0~100）。"""
    if i < TREND_LOOKBACK - 1 or rs is None:
        return False
    close, high, low = bars.close, bars.high, bars.low
    current = float(close[i])
    ma50 = float(np.mean(close[i - 49 : i + 1]))
    ma150 = float(np.mean(close[i - 149 : i + 1]))
    ma200 = float(np.mean(close[i - 199 : i + 1]))
    # MA200 的「20 个交易日前」= 窗口右端退到 i-20（含），即 [i-219, i-19)
    ma200_prev = float(np.mean(close[i - 219 : i - 19]))

    if not (current > ma150 and current > ma200 and ma150 > ma200):
        return False
    if ma200 <= ma200_prev:
        return False
    if not (ma50 > ma150 and current > ma50):
        return False

    low52 = float(np.min(low[i - 249 : i + 1]))
    high52 = float(np.max(high[i - 249 : i + 1]))
    if low52 <= 0 or high52 <= 0:
        return False
    if current < low52 * 1.3 or current < high52 * 0.75:
        return False

    return rs >= TREND_RS_MIN
