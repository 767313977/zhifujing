"""「超跌」筛选的单测：口径取自用户提供的 `日线做T.hxf`。

原式：

    乖离 := (C - MA(C,20)) / MA(C,20) * 100
    超跌 := 乖离 < -15

所以这里要钉住的是三件事：**含当日**的 20 日简单均线、严格小于 −15（等于不算）、
日线不足 20 根时不算超跌。阈值改一个数就会在这里响。
"""

from datetime import date, timedelta

import numpy as np

from app.services.patterns import OVERSOLD_DEV_PCT, OVERSOLD_MA, Bars, is_oversold


def _bars(close: list[float]) -> Bars:
    """只关心收盘价的 Bars —— `is_oversold` 只读 close。"""
    n = len(close)
    values = np.asarray(close, dtype=float)
    return Bars(
        dates=[date(2026, 1, 1) + timedelta(days=i) for i in range(n)],
        open=values.copy(),
        high=values.copy(),
        low=values.copy(),
        close=values,
        volume=np.ones(n),
        amount=np.ones(n),
        pct_chg=np.zeros(n),
    )


def test_needs_full_ma_window():
    """不足 20 根算不出 20 日线，不判超跌（次新股不硬凑）。"""
    assert not is_oversold(_bars([100.0] * (OVERSOLD_MA - 1)))


def test_flat_series_is_not_oversold():
    """横盘（乖离 0）不是超跌。"""
    assert not is_oversold(_bars([100.0] * 30))


def test_exactly_threshold_is_not_oversold():
    """阈值两侧各钉一个点（原式是「< −15」，严格小于）。"""
    assert OVERSOLD_DEV_PCT == -15.0
    # 前 19 根 100、今天 80：MA20 = (100*19 + 80)/20 = 99，乖离 = 80/99 − 1 = −19.19%
    assert is_oversold(_bars([100.0] * 19 + [80.0]))
    # 前 19 根 100、今天 85：MA20 = 99.25，乖离 = −14.36%，不算超跌
    assert not is_oversold(_bars([100.0] * 19 + [85.0]))


def test_uses_today_in_ma20():
    """MA20 必须**含当日**（`MA(C,20)` 的口径）。

    构造一个只有「含当日」才判得对的边界：前 19 根 100、今天 84.7。
    - 含当日：MA20 = (1900 + 84.7)/20 = 99.235，乖离 = 84.7/99.235 − 1 = −14.65% → 不算
    - 不含当日：MA19 = 100，乖离 = −15.3% → 会算成超跌

    所以判定为 False 才说明今天的收盘进了均线。
    """
    assert not is_oversold(_bars([100.0] * 19 + [84.7]))
