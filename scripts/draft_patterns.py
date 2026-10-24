"""实验形态：口袋支点（Pocket Pivot）两个版本。

**不进生产注册表**（`backend/app/services/patterns.py` 的 `PATTERNS`）——
它只被 `scripts/backtest_patterns.py` 引用，先回测对比，达标了再谈上线。

## 两个版本

1. `pocket_pivot_kacher` —— Kacher / Morales 原版（欧奈尔弟子），本仓库按用户
   2026-10-24 给的定义实现：
   - 当天上涨；
   - 当日成交量 > **前 10 个交易日里所有下跌日**的最大成交量；
   - 股价在 10 日或 50 日均线附近/上方，且离最近那条均线不超过 8%（不追高）；
   - 之前有一段整理（20 日振幅 ≤25%）。

2. `pocket_pivot_ths` —— 网上流传的通达信版本，用户在清单里写的是
   「成交额 ≥ 10 日最高 + 涨幅 > 5% + 站上 90 日线」。

⚠️ 原版里「附近」「整理」都没有公认的数值阈值，这里的 8% / 25% 是**实现假设**，
回测时要连同参数一起看；不是原作者的官方参数。
"""

import numpy as np

from app.services.patterns import Bars, Pattern, Signal

#: 原版：成交量对比的回看天数（前 10 个交易日）
POCKET_PRIOR_DAYS = 10
#: 原版：整理窗口（不含当天）
POCKET_BASE_DAYS = 20
#: 原版：整理窗口的最大振幅（25%）
POCKET_BASE_RANGE = 0.25
#: 原版：允许收盘略微跌破均线的幅度（2%）——「附近」的容差
POCKET_NEAR_MA = 0.02
#: 原版：距最近均线的最大距离（8%）——「别追高」
POCKET_MAX_MA_DIST = 0.08


def _mean(values: np.ndarray) -> float:
    return float(np.mean(values))


def pocket_pivot_kacher(bars: Bars) -> Signal | None:
    """原版口袋支点。需要至少 60 根 K 线（MA50 + 前 10 日 + 整理窗口）。"""
    n = len(bars)
    if n < 60:
        return None
    t = n - 1
    close, volume = bars.close, bars.volume

    if close[t] <= close[t - 1]:
        return None

    prior = range(t - POCKET_PRIOR_DAYS, t)
    down_volumes = [
        float(volume[index])
        for index in prior
        if close[index] < close[index - 1]
    ]
    # 前 10 日一个下跌日都没有时，「大于所有下跌日最大量」无从谈起 —— 不命中
    if not down_volumes:
        return None
    down_max = max(down_volumes)
    if float(volume[t]) <= down_max:
        return None

    ma10 = _mean(close[t - 9 : t + 1])
    ma50 = _mean(close[t - 49 : t + 1])
    current = float(close[t])
    if ma10 <= 0 or ma50 <= 0:
        return None
    near10 = current >= ma10 * (1 - POCKET_NEAR_MA)
    near50 = current >= ma50 * (1 - POCKET_NEAR_MA)
    if not (near10 or near50):
        return None
    distance = min(abs(current / ma10 - 1), abs(current / ma50 - 1))
    if distance > POCKET_MAX_MA_DIST:
        return None

    high = float(np.max(bars.high[t - POCKET_BASE_DAYS : t]))
    low = float(np.min(bars.low[t - POCKET_BASE_DAYS : t]))
    if low <= 0 or high / low - 1 > POCKET_BASE_RANGE:
        return None

    volume_ratio = float(volume[t]) / down_max if down_max else None
    return Signal(
        pattern="pocket_pivot_kacher",
        score=100.0,
        detail={
            "量/下跌日最大量": round(volume_ratio, 2) if volume_ratio else 0.0,
            "距MA10": round((current / ma10 - 1) * 100, 2),
            "距MA50": round((current / ma50 - 1) * 100, 2),
            "整理振幅": round((high / low - 1) * 100, 2),
        },
    )


def pocket_pivot_ths(bars: Bars) -> Signal | None:
    """论坛版：涨幅 >5% + 成交额 ≥ 前 10 日最高 + 收盘站上 MA90。"""
    n = len(bars)
    if n < 91:
        return None
    t = n - 1
    close, amount = bars.close, bars.amount

    if float(bars.pct_chg[t]) <= 5:
        return None
    prior_max = float(np.max(amount[t - POCKET_PRIOR_DAYS : t]))
    if float(amount[t]) < prior_max:
        return None
    ma90 = _mean(close[t - 89 : t + 1])
    if ma90 <= 0 or float(close[t]) <= ma90:
        return None

    return Signal(
        pattern="pocket_pivot_ths",
        score=100.0,
        detail={
            "涨幅": round(float(bars.pct_chg[t]), 2),
            "量比10日最高": round(float(amount[t]) / prior_max, 2) if prior_max else 0.0,
            "距MA90": round((float(close[t]) / ma90 - 1) * 100, 2),
        },
    )


#: 实验形态注册表（只给回测脚本用）。`group` 写成「实验」，报告里一眼能分辨。
DRAFT_PATTERNS: tuple[Pattern, ...] = (
    Pattern("pocket_pivot_kacher", "口袋支点(原版)", "实验", pocket_pivot_kacher),
    Pattern("pocket_pivot_ths", "口袋支点(通达信版)", "实验", pocket_pivot_ths),
)
