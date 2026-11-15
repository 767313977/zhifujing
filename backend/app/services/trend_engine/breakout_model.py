"""Model A：趋势启动（平台突破）。

## 判定顺序（先排除，再评分）

1. 数据质量（根数 / 目标日 / 有限值）→ 不合格直接排除；
2. ST / 北交所 / 流动性 → 硬排除；
3. 结构前提：收盘 > MA20、MA20 不向下、平台宽度合格、平台期停牌不过多；
4. 突破前提：收盘 > 平台高点（平台窗口**排除当日**）且不追高；
5. 量价确认：成交额比（分母不含当日）、收盘位置、上影线；
6. 打分：结构 / 突破 / 量价 + RPS 3 分 + 板块 0 分（均可配置）。

## 平台高点为什么必须排除当日

若把当日最高价算进平台，突破当天会「自己突破自己」：平台高点被当天抬到收盘上方，
任何上涨都能被解释成突破，且回测里等于用了未来信息。这里统一用
`high[-days-1:-1]`（`trade_days` 给定时按交易日切片），并在返回里带上窗口起止日。

## 只做收盘确认

本模型只处理**盘后**数据。盘中突破不算信号；`close` 与平台高点的关系是唯一的正式判据。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np

from app.services import limit_rules
from app.services.patterns import Bars, is_st
from app.services.trend_engine import config as cfg_module
from app.services.trend_engine import explanation, indicators, momentum, scoring, trend_structure, volume_price


@dataclass(frozen=True, slots=True)
class ModelAResult:
    code: str
    name: str | None
    trade_date: date | None
    eligible: bool
    score: float
    parts: dict[str, float | None]
    tier: str
    platform_high: float | None = None
    platform_low: float | None = None
    platform_width_pct: float | None = None
    platform_days: int | None = None
    platform_start: date | None = None
    breakout_pct: float | None = None
    volume_confirm: bool | None = None
    structure: trend_structure.TrendStructure | None = None
    momentum: momentum.MomentumState | None = None
    volume: volume_price.VolumePriceState | None = None
    reasons: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    model_version: str = cfg_module.MODEL_A_VERSION
    calc_version: str = cfg_module.CALC_VERSION


TIERS = (
    (80.0, "重点启动候选"),
    (70.0, "启动观察"),
    (60.0, "等待确认"),
    (0.0, "不进入核心池"),
)


def _platform(
    bars: Bars,
    config: cfg_module.PlatformConfig,
    trade_days: list[date] | None,
) -> tuple[float | None, float | None, int, date | None, tuple[str, ...]]:
    """返回 (平台高, 平台低, 实际天数, 起点, 缺失/问题)。窗口**不含当日**。"""
    if trade_days:
        window = trade_days[-(config.days + 1) : -1]
        index = {day: pos for pos, day in enumerate(bars.dates)}
        positions = [index[day] for day in window if day in index]
        coverage = len(positions) / len(window) if window else 0.0
        if len(window) < config.min_days:
            return None, None, len(positions), None, ("平台观察窗口不足",)
        if coverage < 0.9:
            return None, None, len(positions), None, (
                f"平台期停牌过多（覆盖 {coverage * 100:.0f}%）",
            )
        if not positions:
            return None, None, 0, None, ("平台期没有可用日线",)
        high = float(np.max(bars.high[positions]))
        low = float(np.min(bars.low[positions]))
        return high, low, len(positions), window[0], ()

    if len(bars) - 1 < config.min_days:
        return None, None, max(len(bars) - 1, 0), None, ("平台观察窗口不足",)
    days = min(config.days, len(bars) - 1)
    chunk_high = bars.high[-days - 1 : -1]
    chunk_low = bars.low[-days - 1 : -1]
    high = float(np.max(chunk_high))
    low = float(np.min(chunk_low))
    return high, low, days, bars.dates[-days - 1], ()


def _structure_part(
    structure: trend_structure.TrendStructure,
    convergence: float | None,
    config: cfg_module.ModelAConfig,
) -> float:
    """结构分项（0~1）：中期趋势正在形成，而不是已经走完。"""
    close_above = 1.0 if structure.above_ma20 else 0.0
    ma20_turn = {"up": 1.0, "flat": 0.5}.get(structure.ma20_turn or "", 0.0)
    ma60_ok = 1.0 if structure.ma60_turn in ("up", "flat") else 0.0
    gap_ok = 1.0 if (structure.gap_ma20_ma60_pct or -1) > 0 else 0.0
    trend_ok = scoring.gate_score(structure.trend_days, 0, 10)
    converge_ok = scoring.gate_score(
        convergence, config.max_platform_convergence_pct, 1.0
    )
    return (
        close_above * 0.25
        + ma20_turn * 0.25
        + ma60_ok * 0.15
        + gap_ok * 0.15
        + trend_ok * 0.10
        + converge_ok * 0.10
    )


def _breakout_part(
    width_pct: float | None,
    breakout_pct: float | None,
    close_position: float | None,
    platform_days: int,
    config: cfg_module.ModelAConfig,
) -> float:
    """突破质量（0~1）：平台越窄、突破幅度适中、收盘越靠上，分越高。"""
    width_score = 1.0 - scoring.gate_score(
        width_pct, config.platform.ideal_width_pct, config.platform.max_width_pct
    )
    buffer_score = scoring.band_score(
        breakout_pct, 0.0, max(config.platform.max_breakout_excess_pct * 0.6, 0.1)
    )
    position_score = scoring.gate_score(close_position, 0.3, 0.9)
    length_score = scoring.gate_score(platform_days, config.platform.min_days, config.platform.days)
    return width_score * 0.35 + buffer_score * 0.25 + position_score * 0.25 + length_score * 0.15


def _volume_part(state: volume_price.VolumePriceState, config: cfg_module.ModelAConfig) -> float:
    """量价配合（0~1）：放量但不过分、量能有持续性、上涨日量能占优。"""
    if state.amount_ratio20 is None:
        return 0.0
    ratio_score = scoring.band_score(
        state.amount_ratio20,
        config.min_breakout_amount_ratio,
        config.ideal_breakout_amount_ratio,
    )
    trend_score = scoring.gate_score(state.amount_trend_ratio, 0.6, 1.0)
    updown_score = scoring.gate_score(state.up_down_volume_ratio20, 0.8, 1.5)
    close_score = scoring.gate_score(state.close_position, 0.4, 0.9)
    return ratio_score * 0.4 + trend_score * 0.2 + updown_score * 0.2 + close_score * 0.2


def _rps_part(state: momentum.MomentumState) -> float | None:
    """RPS 分项（0~1）。四档都缺失时返回 None（不给 0 分冒充「弱」）。"""
    values = [state.rps.get("rps20"), state.rps.get("rps60"), state.rps.get("rps120")]
    weights = (0.4, 0.4, 0.2)
    total = 0.0
    used = 0.0
    for value, weight in zip(values, weights):
        if value is None:
            continue
        total += (float(value) / 100.0) * weight
        used += weight
    if used <= 0:
        return None
    return total / used


def evaluate(
    bars: Bars,
    *,
    code: str = "",
    name: str | None = None,
    day: date | None = None,
    rps_values: dict[str, float | None] | None = None,
    rps_history: list[tuple[date, dict[str, float | None]]] | None = None,
    sector_score: float | None = None,
    trade_days: list[date] | None = None,
    config: cfg_module.ModelAConfig | None = None,
    exclude_st: bool = True,
    exclude_bse: bool = True,
) -> ModelAResult:
    config = config or cfg_module.ModelAConfig()
    weights = cfg_module.effective_weights(
        config.base_weights, config.rps_points, config.sector_points
    )
    target = day or (bars.dates[-1] if bars.dates else None)
    quality = indicators.evaluate_quality(bars, target, config.indicator.min_bars)
    if not quality.ok:
        return ModelAResult(
            code=code or bars.code,
            name=name,
            trade_date=target,
            eligible=False,
            score=0.0,
            parts={},
            tier=TIERS[-1][1],
            exclusions=quality.missing,
            missing=quality.missing,
        )

    exclusions: list[str] = []
    risks: list[str] = []
    missing: list[str] = list(quality.missing)

    if exclude_st and is_st(name):
        exclusions.append("ST 股票")
    if exclude_bse and code and limit_rules.is_bse(code):
        exclusions.append("北交所股票")

    structure = trend_structure.evaluate(bars, config.indicator)
    mom = momentum.evaluate(
        bars, rps_values, rps_history=rps_history, config=config.indicator
    )
    volume = volume_price.evaluate(bars, config)
    missing.extend(item for item in structure.missing if item not in missing)
    missing.extend(item for item in mom.missing if item not in missing)
    missing.extend(item for item in volume.missing if item not in missing)

    # ---- 流动性：前 20 日日均成交额（不含当日）----
    amount_base = None
    if len(bars.amount) >= 21 and np.all(np.isfinite(bars.amount[-21:-1])):
        amount_base = float(np.mean(bars.amount[-21:-1]))
    if amount_base is None:
        exclusions.append("成交额数据不足")
    elif amount_base < config.min_amount20:
        exclusions.append(f"流动性不足（20 日均额 {amount_base / 1e8:.2f} 亿）")

    # ---- 平台与突破 ----
    platform_high, platform_low, platform_days, platform_start, platform_issues = _platform(
        bars, config.platform, trade_days
    )
    exclusions.extend(platform_issues)
    width_pct = None
    if platform_high is not None and platform_low is not None and platform_low > 0:
        width_pct = round((platform_high / platform_low - 1) * 100, 2)
        if width_pct > config.platform.max_width_pct:
            exclusions.append(f"平台过宽（{width_pct:.1f}%）")

    close = float(bars.close[-1])
    breakout_pct = None
    if platform_high is not None and platform_high > 0:
        breakout_pct = round((close / platform_high - 1) * 100, 2)
        if breakout_pct <= config.platform.min_breakout_buffer_pct:
            exclusions.append(f"未突破平台高点 {platform_high:.2f}")
        elif breakout_pct > config.platform.max_breakout_excess_pct:
            exclusions.append(f"已过度偏离平台高点（+{breakout_pct:.1f}%）")

    # ---- 结构前提 ----
    if structure.above_ma20 is False:
        exclusions.append("收盘低于 MA20")
    if structure.ma20_turn == "down":
        exclusions.append("MA20 仍在向下")
    if structure.close is not None and structure.distance_ma20_pct is not None:
        if structure.distance_ma20_pct > config.max_extension_above_ma20_pct:
            exclusions.append(f"距 MA20 乖离过大（{structure.distance_ma20_pct:.1f}%）")

    convergence = None
    if len(bars) >= 61:
        convergence = indicators.convergence_pct(
            bars.close[:-1], (5, 10, 20, 60)
        )

    # ---- 量价确认与排除 ----
    volume_confirm = None
    if volume.amount_ratio20 is not None and volume.close_position is not None:
        volume_confirm = bool(
            volume.amount_ratio20 >= config.min_breakout_amount_ratio
            and volume.close_position >= config.min_close_position
        )
    if volume.amount_ratio20 is None:
        exclusions.append("当日成交额缺失")
    elif volume.amount_ratio20 < config.min_breakout_amount_ratio:
        exclusions.append(
            f"突破日未放量（{volume.amount_ratio20:.2f}× < {config.min_breakout_amount_ratio:.1f}×）"
        )
    if volume.close_position is not None and volume.close_position < config.min_close_position:
        exclusions.append(f"收盘位置偏低（{volume.close_position:.2f}）")
    if volume.upper_shadow_pct is not None and volume.upper_shadow_pct > config.max_upper_shadow_pct:
        exclusions.append(f"放量长上影（上影占振幅 {volume.upper_shadow_pct:.0f}%）")
    if volume.divergence == "price_up_volume_down":
        exclusions.append("量价背离：价涨量缩")
    if volume.abnormal_amount:
        risks.append(f"单日成交额异常放大（{volume.amount_ratio20:.1f}×），不作为加分项")

    # ---- 单日脉冲 / 高位加速 ----
    pct_today = float(bars.pct_chg[-1]) if len(bars.pct_chg) else 0.0
    if pct_today > 9.5 and (volume.close_position or 0) < 0.5:
        exclusions.append("单日脉冲上涨且收盘回落")
    if (mom.ret.get(20) or 0) > 40 and (structure.distance_ma20_pct or 0) > 10:
        exclusions.append("高位连续加速（20 日涨幅 >40% 且乖离 >10%）")

    # ---- 打分 ----
    parts: dict[str, float | None] = {
        "structure": _structure_part(structure, convergence, config),
        "breakout": _breakout_part(width_pct, breakout_pct, volume.close_position, platform_days, config),
        "volume": _volume_part(volume, config),
        "rps": _rps_part(mom),
        "sector": None if sector_score is None else float(sector_score) / 100.0,
    }
    total, missing_parts = scoring.weighted_total(parts, weights)
    missing.extend(part for part in missing_parts if part not in missing)
    if missing_parts:
        risks.append(f"分项缺失：{', '.join(missing_parts)}（按 0 分计入）")

    eligible = not exclusions
    score = total if eligible else 0.0
    level = scoring.tier(score, TIERS) if eligible else TIERS[-1][1]

    # ---- 解释 ----
    reasons: list[str] = []
    if eligible:
        if structure.ma20_turn == "up":
            reasons.append(f"MA20 5 日斜率转正（{explanation.pct(structure.slope.get(20))}）")
        if structure.trend_days:
            reasons.append(f"收盘站上 MA20 已 {structure.trend_days} 个交易日")
        if convergence is not None:
            reasons.append(f"均线收敛度 {convergence:.2f}%（越小越粘合）")
        if platform_high is not None:
            reasons.append(
                f"突破 {platform_days} 日平台高点 {platform_high:.2f}（{explanation.pct(breakout_pct)}）"
            )
        if volume.amount_ratio20 is not None:
            reasons.append(f"成交额为前 20 日均值的 {explanation.ratio(volume.amount_ratio20)}")
        if volume.close_position is not None:
            reasons.append(f"收盘位于当日振幅 {volume.close_position * 100:.0f}%")
        rps20 = mom.rps.get("rps20")
        if rps20 is not None:
            reasons.append(f"RPS20 {rps20:.1f}（横截面分位）")
        if sector_score is not None:
            reasons.append(f"所属板块强度 {sector_score:.0f}/100")

    if structure.ma60_turn == "down":
        risks.append("MA60 仍向下：中期趋势尚未完全转向")
    if (structure.distance_ma20_pct or 0) > 8:
        risks.append(f"距 MA20 乖离 {structure.distance_ma20_pct:.1f}%，注意回踩")
    if width_pct is not None and width_pct > config.platform.ideal_width_pct:
        risks.append(f"平台宽度 {width_pct:.1f}%，整理不够充分")
    if mom.rps.get("rps60") is None:
        risks.append("RPS60 缺失，相对强度无法确认")
    if (volume.upper_shadow_pct or 0) > 30:
        risks.append(f"上影线占振幅 {volume.upper_shadow_pct:.0f}%，警惕冲高回落")

    return ModelAResult(
        code=code or bars.code,
        name=name,
        trade_date=target,
        eligible=eligible,
        score=score,
        parts=parts,
        tier=level,
        platform_high=platform_high,
        platform_low=platform_low,
        platform_width_pct=width_pct,
        platform_days=platform_days,
        platform_start=platform_start,
        breakout_pct=breakout_pct,
        volume_confirm=volume_confirm,
        structure=structure,
        momentum=mom,
        volume=volume,
        reasons=explanation.collect(tuple(reasons)),
        exclusions=explanation.collect(tuple(exclusions)),
        risks=explanation.collect(tuple(risks)),
        missing=explanation.collect(tuple(missing)),
    )
