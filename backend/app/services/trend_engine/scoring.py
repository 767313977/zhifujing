"""评分工具：分项 0~1 映射 + 加权总分 + 缺失显式化。

原则（任务书第九节）：

1. 分项透明：每个分项都必须能单独读出数值与对应指标；
2. 缺失不给分：某个分项缺输入时该分项记 0，并把缺失写进 `missing`；
3. 未知不是低风险：硬性排除与评分分离，数据不足的票由模型直接判 `eligible=False`；
4. 总分恒为 100（卫星项从基础权重里扣，不额外膨胀）。
"""

from __future__ import annotations

import math


def clamp01(value: float | None) -> float:
    if value is None or not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def gate_score(value: float | None, gate: float, ideal: float) -> float:
    """过门槛 0 分、到理想值 1 分的线性映射（方向由 gate/ideal 大小决定）。

    与 `patterns._gate_score` 同一思路，但返回 0~1 比例，由调用方乘分项满分。
    """
    if value is None or not math.isfinite(value):
        return 0.0
    if gate == ideal:
        return 1.0
    ratio = (value - gate) / (ideal - gate)
    return clamp01(ratio)


def band_score(
    value: float | None,
    low: float,
    high: float,
    *,
    edge: float = 0.6,
) -> float:
    """区间内满分、区间外线性衰减（两端都不好时用，如放量倍数、乖离率）。

    `edge` 是区间边界处的分数（默认 0.6）；离区间越远越低，最低 0。
    """
    if value is None or not math.isfinite(value):
        return 0.0
    if low > high:
        low, high = high, low
    if low <= value <= high:
        return 1.0
    span = high - low
    if span <= 0:
        span = max(abs(high), 1.0) * 0.1
    distance = (low - value) if value < low else (value - high)
    return clamp01(edge * (1 - distance / span))


def weighted_total(
    parts: dict[str, float | None],
    weights: dict[str, float],
) -> tuple[float, tuple[str, ...]]:
    """把分项（0~1）按权重（分）加总。分项为 None 时按 0 计入并登记缺失。"""
    total = 0.0
    missing: list[str] = []
    for key, weight in weights.items():
        if weight <= 0:
            # 权重为 0 的卫星项（如默认关闭的板块）不参与计分，也不算「缺失」
            continue
        value = parts.get(key)
        if value is None:
            missing.append(key)
            continue
        total += clamp01(value) * weight
    return round(total, 1), tuple(missing)


def tier(score: float, thresholds: tuple[tuple[float, str], ...]) -> str:
    """按 (下限, 名称) 从高到低匹配分层。"""
    for minimum, name in thresholds:
        if score >= minimum:
            return name
    return thresholds[-1][1] if thresholds else "unknown"
