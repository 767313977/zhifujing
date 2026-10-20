"""「涨停爆量横盘」的极限数据回归测试。

2026-10-20 的长历史（1230 个交易日）回测跑了一半崩在：

    keep = (flat_low - platform) / (seg_high - platform)
    ZeroDivisionError: float division by zero

一崩整个扫描就退出（2000/4922 只，白跑 3 小时）。这里钉住「分母为 0 时跳过、
不抛异常」这一条，防止以后再靠跑几个小时才发现。
"""

import numpy as np

from app.services.patterns import Bars, _limit_surge_flat


def _zero_denominator_bars() -> Bars:
    """构造 `seg_high == platform` 的一轮：涨停日最高价恰好等于前 40 日平台最高价。

    真实行情里 close ≤ high，所以「收盘突破平台」与「seg_high == platform」是矛盾的；
    这里按异常数据构造（涨停日 close 高于 high），只为了把分母打到 0 —— 长历史库
    里正是这种行让回测崩掉的。
    """
    size = 60
    high = np.full(size, 100.0)  # 前 40 日平台高 = 涨停日最高价 = 100
    low = np.full(size, 98.0)
    close = np.full(size, 84.0)  # 涨停前收盘 84 → 上冲到 100 是 +19%（过 LS_MIN_RUN）
    close[50] = 109.0  # 涨停日收盘（异常地高于 high，只为过「收盘突破平台」）
    close[51:] = 98.5  # 横盘段
    volume = np.full(size, 1000.0)
    volume[50] = 3000.0  # 涨停爆量：3 倍于前 5 日均量
    volume[51:] = 500.0  # 横盘缩量
    pct = np.zeros(size)
    pct[50] = 10.0
    return Bars(
        dates=[__import__("datetime").date(2025, 1, 1)] * size,
        open=close.copy(),
        high=high,
        low=low,
        close=close,
        volume=volume,
        amount=volume * close,
        pct_chg=pct,
    )


def test_zero_denominator_skips_instead_of_crashing():
    """`seg_high - platform == 0` 时直接跳过这一轮，返回 None 而不是抛零除。"""
    assert _limit_surge_flat(_zero_denominator_bars()) is None
