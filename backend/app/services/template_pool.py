"""样板池：判定「有量冲高又收回来」的样板日（量价结构选股的第一天）。

这套口径来自一份外部的短线选股纪律 —— **「样板日 → 启动日」两日节奏**：

- **样板日**（D 日）：最高价对昨收冲高足够、量比够，且**收盘明显离开最高**
  （不是封死、不是单边）→ 记下「今高」，次日就盯这条线。
- **启动日**（D+1）：放量越过昨高、收盘站得住 → 「今天可买」。

本站只做前半段：收盘后筛出合格的样板日，输出次日的**盯盘清单**
（触发价 = 今高、兜底线 = 今高 × 0.98）。后半段按定义要「盘中刚过昨高」那一刻
的确认，需要盘中实时数据 —— 交给人眼，见设计文档 8.49。

## 量比口径

`量比 = 当日成交量 ÷ 前 20 个交易日平均量`（**不含当日**，也不和昨天比）。
不含当日有两个理由：① 站内只有一份量比口径（`patterns.volume_ratio`，形态引擎
按 5 日算的那个也是它），两个页面各算一套迟早打架；② 含当日会让「今天量越大 →
分母越大」，把最该看见的爆量日自己压下去。

## 两处「原文只有形容词」的地方，本实现必须自己量化

原文对样板日的第 3 条只写了「收盘明显离开最高 / 上影足够，不能封死单边」，
没有给数。这里取**收盘在当日区间里的位置**：

    收盘位置 = (收盘 - 最低) / (最高 - 最低)   ← ≤ 0.7 才算「离开最高」

选它而不是另写一套「上影 / 振幅」的理由：两者是同一件事的两种写法（都等于
「收盘离最高有多远」），留一个就没有第二个可拧的旋钮；而且它顺带把原文的
「近似封死最高」也盖住了 —— 收在区间顶部、涨幅又大的票本来就被这一条挡掉。

## 阈值全部带「约」字

7% / 1.3 / 0.7 / 12% / 18% / -3% 都是经验值，不是拟合出来的参数。边界样本
（量比 1.28、冲高 6.9%）会来回翻结论，这是这套口径的固有性质，不要试图
把它当成精确信号。
"""

import logging
from dataclasses import dataclass

from app.services.patterns import Bars, volume_ratio

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 样板日阈值

# 冲高足够：最高价相对**昨收**的涨幅（不是收盘涨幅）
TEMPLATE_SURGE_MIN = 0.07
# 量够
TEMPLATE_VOL_RATIO_MIN = 1.3
# 量比窗口，与原文一致（近 20 个交易日）
TEMPLATE_VOL_WINDOW = 20

# 收盘离开最高：收盘位置的上限（见模块说明）
TEMPLATE_CLOSE_POS_MAX = 0.7

# 排除项之一：收盘已经是大阳主升
TEMPLATE_PCT_MAX = 12.0
# 排除项之二：冲高已经接近封板（主板 10% / 创业板 20% 都够不着「样板」了）
TEMPLATE_SURGE_MAX = 0.18
# 排除项之三：收盘大跌
TEMPLATE_PCT_FLOOR = -3.0

# 判定最少要多少根 K 线：20 日均量 + 昨收 + 今天
TEMPLATE_MIN_BARS = TEMPLATE_VOL_WINDOW + 2


@dataclass(slots=True)
class TemplateHit:
    """合格样板日的三个派生量（原始价格由调用方从最后一根 K 线取）。"""

    # 冲高幅度（比值，0.098 = 9.8%）
    surge: float
    # 量比（当日量 / 前 20 日均量）
    vol_ratio: float
    # 收盘在当日区间里的位置（0 = 收在最低，1 = 收在最高）
    close_pos: float


def evaluate(bars: Bars) -> TemplateHit | None:
    """判定最后一根 K 线是不是合格的样板日，不是就给 None。

    只读日线，**不看任何公告或消息** —— 这套口径的核心信念就是「只认画线 +
    过线」，不靠消息猜涨。
    """
    if len(bars) < TEMPLATE_MIN_BARS:
        return None

    high = float(bars.high[-1])
    low = float(bars.low[-1])
    close = float(bars.close[-1])
    prev_close = float(bars.close[-2])
    pct = float(bars.pct_chg[-1])
    if high <= 0 or prev_close <= 0:
        return None

    # 1) 冲高足够，且还没冲到「已经是主升」的地步
    surge = high / prev_close - 1
    if surge < TEMPLATE_SURGE_MIN or surge >= TEMPLATE_SURGE_MAX:
        return None

    # 2) 收盘不能已经是大阳主升，也不能大跌
    if pct >= TEMPLATE_PCT_MAX or pct < TEMPLATE_PCT_FLOOR:
        return None

    # 3) 收盘必须离开最高。区间为 0（一字）时无从谈起，直接不算
    span = high - low
    if span <= 0:
        return None
    close_pos = (close - low) / span
    if close_pos > TEMPLATE_CLOSE_POS_MAX:
        return None

    # 4) 量够
    ratio = volume_ratio(bars, TEMPLATE_VOL_WINDOW)
    if ratio < TEMPLATE_VOL_RATIO_MIN:
        return None

    return TemplateHit(surge=surge, vol_ratio=ratio, close_pos=close_pos)
