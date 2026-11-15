"""Model B：趋势延续（中期趋势 + 合理回调 + 再次转强）。

与 Model A 的分工：

- A 看「刚刚突破平台」，需要放量与窄平台；
- B 看「已经走了一段、回调后结构没坏」，需要均线多头、回调适度、缩量、支撑有效；
- **B 不复用 A 的突破评分**，两套分项独立。

例外处理：MA60 向下、MA60 ≤ MA120 都属于「趋势改善型例外」——
允许入选但必须写进 `exception_flags` 与风险提示，不能靠降低阈值把它藏起来。
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
class PullbackState:
    """回调结构。`depth_pct` 为峰到谷的跌幅（正数，%）。"""

    peak_index: int | None = None
    peak_price: float | None = None
    trough_price: float | None = None
    depth_pct: float | None = None
    atr_multiple: float | None = None
    pullback_days: int | None = None
    #: 回调期均额 / 上涨期均额（越小越缩量）
    volume_shrink_ratio: float | None = None
    #: 回调低点相对当时 MA20 的乖离（%），负值表示跌破
    trough_vs_ma20_pct: float | None = None
    support_price: float | None = None
    rebound_confirmed: bool | None = None
    missing: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelBResult:
    code: str
    name: str | None
    trade_date: date | None
    eligible: bool
    score: float
    parts: dict[str, float | None]
    tier: str
    structure: trend_structure.TrendStructure | None = None
    momentum: momentum.MomentumState | None = None
    volume: volume_price.VolumePriceState | None = None
    pullback: PullbackState | None = None
    #: 趋势强度：结构分项 × 100（不是概率，只用于同批比较）
    trend_strength: float | None = None
    exception_flags: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    model_version: str = cfg_module.MODEL_B_VERSION
    calc_version: str = cfg_module.CALC_VERSION


TIERS = (
    (85.0, "核心趋势观察"),
    (75.0, "趋势延续候选"),
    (65.0, "等待确认"),
    (0.0, "不进入核心池"),
)


def _pullback(
    bars: Bars,
    config: cfg_module.PullbackConfig,
    atr_pct_value: float | None,
) -> PullbackState:
    close = bars.close
    amount = bars.amount
    missing: list[str] = []
    if len(close) < config.lookback_days + 1:
        return PullbackState(missing=("回调观察窗口不足",))

    window = close[-(config.lookback_days + 1) :]
    offset = len(close) - window.size
    # 最近一次「已完成的回撤」：以窗口内运行最高点为基准，找最大回撤的那一天作为谷。
    # 不能简单取窗口最高点当峰 —— 反弹创新高后，峰会变成今天，回撤被算成 0。
    running_peak = np.maximum.accumulate(window)
    drawdown = window / np.where(running_peak > 0, running_peak, np.nan) - 1
    trough_local = int(np.nanargmin(drawdown))
    peak_local = int(np.argmax(window[: trough_local + 1]))
    peak_index = offset + peak_local
    trough_index = offset + trough_local
    peak = float(close[peak_index])
    trough = float(close[trough_index])
    if peak <= 0:
        return PullbackState(missing=("峰值价格异常",))

    depth = round((1 - trough / peak) * 100, 2)
    atr_multiple = None
    if atr_pct_value and atr_pct_value > 0:
        atr_multiple = round(depth / atr_pct_value, 2)
    else:
        missing.append("ATR%")

    pullback_days = len(close) - 1 - peak_index
    shrink = None
    if pullback_days >= 3:
        up_start = max(peak_index - 20, 0)
        up_amount = amount[up_start : peak_index + 1]
        down_amount = amount[peak_index + 1 : trough_index + 1]
        if up_amount.size and down_amount.size:
            up_mean = float(np.mean(up_amount))
            down_mean = float(np.mean(down_amount))
            if up_mean > 0:
                shrink = round(down_mean / up_mean, 2)
    else:
        missing.append("回调天数不足 3 天")

    ma20_series = indicators.rolling_mean(close, 20)
    trough_ma20 = float(ma20_series[trough_index]) if np.isfinite(ma20_series[trough_index]) else None
    trough_vs_ma20 = None
    if trough_ma20 is not None and trough_ma20 > 0:
        trough_vs_ma20 = round((trough / trough_ma20 - 1) * 100, 2)
    else:
        missing.append("回调期 MA20")

    ma5 = indicators.ma_last(close, 5)
    rebound = None
    if ma5 is not None and len(close) >= 2 and pullback_days >= 1 and trough_index < len(close) - 1:
        pullback_amount = amount[peak_index + 1 : trough_index + 1]
        base = float(np.mean(pullback_amount)) if pullback_amount.size else None
        if base and base > 0:
            rebound = bool(
                float(close[-1]) > ma5
                and float(close[-1]) > float(close[trough_index])
                and float(amount[-1]) / base >= 1.0
            )

    return PullbackState(
        peak_index=peak_index,
        peak_price=round(peak, 4),
        trough_price=round(trough, 4),
        depth_pct=depth,
        atr_multiple=atr_multiple,
        pullback_days=pullback_days,
        volume_shrink_ratio=shrink,
        trough_vs_ma20_pct=trough_vs_ma20,
        support_price=round(max(trough, trough_ma20 or trough), 4),
        rebound_confirmed=rebound,
        missing=tuple(missing),
    )


def _structure_part(
    structure: trend_structure.TrendStructure, config: cfg_module.ModelBConfig
) -> float:
    ma20_above_60 = 1.0 if (structure.gap_ma20_ma60_pct or -1) > 0 else 0.0
    ma60_above_120 = None
    if structure.ma.get(60) is not None and structure.ma.get(120) is not None:
        ma60_above_120 = 1.0 if structure.ma[60] > structure.ma[120] else 0.0
    ma20_up = {"up": 1.0, "flat": 0.5}.get(structure.ma20_turn or "", 0.0)
    ma60_up = {"up": 1.0, "flat": 0.5}.get(structure.ma60_turn or "", 0.0)
    trend_ok = scoring.gate_score(structure.trend_days, config.min_trend_days, config.min_trend_days * 3)
    close_above = 1.0 if structure.above_ma20 else 0.0
    values = [
        (ma20_above_60, 0.25),
        (ma60_above_120 if ma60_above_120 is not None else 0.0, 0.15),
        (ma20_up, 0.20),
        (ma60_up, 0.15),
        (trend_ok, 0.15),
        (close_above, 0.10),
    ]
    return sum(value * weight for value, weight in values)


def _pullback_part(state: PullbackState, config: cfg_module.PullbackConfig) -> float:
    depth_score = scoring.band_score(
        state.depth_pct, config.min_depth_pct, config.max_depth_pct
    )
    atr_score = scoring.band_score(
        state.atr_multiple, config.min_atr_multiple, config.max_atr_multiple
    )
    shrink_score = scoring.gate_score(
        state.volume_shrink_ratio, 1.2, config.max_volume_shrink_ratio
    )
    if state.trough_vs_ma20_pct is None:
        support_score = 0.0
    elif state.trough_vs_ma20_pct >= 0:
        support_score = 1.0
    else:
        support_score = scoring.clamp01(
            1 - (-state.trough_vs_ma20_pct) / max(config.max_below_ma20_pct, 1e-9)
        )
    rebound_score = 1.0 if state.rebound_confirmed else 0.0
    return (
        depth_score * 0.25
        + atr_score * 0.25
        + shrink_score * 0.20
        + support_score * 0.20
        + rebound_score * 0.10
    )


def _volume_part(state: volume_price.VolumePriceState, config: cfg_module.ModelBConfig) -> float:
    ratio_score = scoring.band_score(state.amount_ratio20, 0.7, 2.0)
    trend_score = scoring.gate_score(state.amount_trend_ratio, 0.5, 1.2)
    updown_score = scoring.gate_score(state.up_down_volume_ratio20, 0.8, 1.5)
    close_score = scoring.gate_score(state.close_position, 0.4, 0.9)
    divergence_score = 0.0 if state.divergence else 1.0
    return (
        ratio_score * 0.25
        + trend_score * 0.20
        + updown_score * 0.20
        + close_score * 0.15
        + divergence_score * 0.20
    )


def _rps_part(state: momentum.MomentumState) -> float | None:
    values = [state.rps.get("rps60"), state.rps.get("rps120"), state.rps.get("rps250")]
    weights = (0.45, 0.35, 0.20)
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
    config: cfg_module.ModelBConfig | None = None,
    exclude_st: bool = True,
    exclude_bse: bool = True,
) -> ModelBResult:
    config = config or cfg_module.ModelBConfig()
    weights = cfg_module.effective_weights(
        config.base_weights, config.rps_points, config.sector_points
    )
    target = day or (bars.dates[-1] if bars.dates else None)
    quality = indicators.evaluate_quality(bars, target, config.indicator.min_bars)
    if not quality.ok:
        return ModelBResult(
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
    exceptions: list[str] = []
    missing: list[str] = list(quality.missing)

    if exclude_st and is_st(name):
        exclusions.append("ST 股票")
    if exclude_bse and code and limit_rules.is_bse(code):
        exclusions.append("北交所股票")

    structure = trend_structure.evaluate(bars, config.indicator)
    mom = momentum.evaluate(bars, rps_values, rps_history=rps_history, config=config.indicator)
    volume = volume_price.evaluate(bars, config)
    atr_pct_value = indicators.atr_pct(bars.high, bars.low, bars.close, config.indicator.atr_window)
    pullback = _pullback(bars, config.pullback, atr_pct_value)

    for item in structure.missing + mom.missing + volume.missing + pullback.missing:
        if item not in missing:
            missing.append(item)

    # ---- 流动性 ----
    if len(bars.amount) >= 21 and np.all(np.isfinite(bars.amount[-21:-1])):
        base_amount = float(np.mean(bars.amount[-21:-1]))
        if base_amount < config.min_amount20:
            exclusions.append(f"流动性不足（20 日均额 {base_amount / 1e8:.2f} 亿）")
    else:
        exclusions.append("成交额数据不足")

    # ---- 中期结构 ----
    if (structure.gap_ma20_ma60_pct or -1) <= 0:
        exclusions.append("MA20 未高于 MA60")
    if structure.ma.get(60) is not None and structure.ma.get(120) is not None:
        if structure.ma[60] <= structure.ma[120]:
            if config.allow_ma60_down_exception:
                exceptions.append("MA60 未高于 MA120（趋势改善型例外）")
            else:
                exclusions.append("MA60 未高于 MA120")
    if structure.ma60_turn == "down":
        if config.allow_ma60_down_exception:
            exceptions.append("MA60 仍向下（趋势改善型例外）")
        else:
            exclusions.append("MA60 明显向下")
    if structure.ma20_turn == "down":
        exclusions.append("MA20 向下")
    if structure.distance_ma20_pct is not None and structure.distance_ma20_pct < -config.pullback.max_below_ma20_pct:
        exclusions.append(f"收盘低于 MA20 {abs(structure.distance_ma20_pct):.1f}%")
    elif structure.above_ma20 is False:
        risks.append("收盘略低于 MA20，仍在测试支撑")
    if structure.trend_days < config.min_trend_days:
        exclusions.append(f"站上 MA20 仅 {structure.trend_days} 天（要求 ≥{config.min_trend_days}）")

    # ---- 回调结构 ----
    if pullback.depth_pct is None:
        exclusions.append("回调结构无法计算")
    else:
        if pullback.depth_pct < config.pullback.min_depth_pct:
            exclusions.append(f"尚未形成有效回调（回撤 {pullback.depth_pct:.1f}%）")
        elif pullback.depth_pct > config.pullback.max_depth_pct:
            exclusions.append(f"回调过深（{pullback.depth_pct:.1f}%）")
        if pullback.atr_multiple is not None:
            if pullback.atr_multiple > config.pullback.max_atr_multiple:
                risks.append(f"ATR 标准化回撤 {pullback.atr_multiple:.2f}× 偏大")
        if pullback.trough_vs_ma20_pct is not None and pullback.trough_vs_ma20_pct < -config.pullback.max_below_ma20_pct:
            risks.append(f"回调低点跌破 MA20 {abs(pullback.trough_vs_ma20_pct):.1f}%")
        if pullback.volume_shrink_ratio is not None and pullback.volume_shrink_ratio > 1.0:
            risks.append(f"回调期未缩量（量比 {pullback.volume_shrink_ratio:.2f}）")
        if pullback.rebound_confirmed is False:
            risks.append("尚未出现再次转强的量价确认")

    # ---- 高位滞涨 / 趋势衰减 ----
    if (
        volume.amount_ratio20 is not None
        and volume.amount_ratio20 > 1.8
        and (bars.pct_chg[-1] if len(bars.pct_chg) else 0) <= 0
        and (volume.close_position or 0) < 0.4
    ):
        exclusions.append("高位放量滞涨")
    rps60 = mom.rps.get("rps60")
    if rps60 is not None and mom.rps60_improve is not None and mom.rps60_improve <= -10:
        risks.append(f"RPS60 近 5 日下降 {abs(mom.rps60_improve):.0f} 分位")
    if (mom.ret.get(20) or 0) < 0:
        risks.append("20 日收益为负，趋势动能在衰减")

    parts: dict[str, float | None] = {
        "structure": _structure_part(structure, config),
        "pullback": _pullback_part(pullback, config.pullback),
        "volume": _volume_part(volume, config),
        "rps": _rps_part(mom),
        "sector": None if sector_score is None else float(sector_score) / 100.0,
    }
    total, missing_parts = scoring.weighted_total(parts, weights)
    for item in missing_parts:
        if item not in missing:
            missing.append(item)
    if missing_parts:
        risks.append(f"分项缺失：{', '.join(missing_parts)}（按 0 分计入）")

    eligible = not exclusions
    score = total if eligible else 0.0
    level = scoring.tier(score, TIERS) if eligible else TIERS[-1][1]
    trend_strength = round((parts["structure"] or 0.0) * 100, 1)

    reasons: list[str] = []
    if eligible:
        reasons.append(f"MA20 > MA60（距离 {explanation.pct(structure.gap_ma20_ma60_pct)}）")
        if structure.ma.get(120) is not None and structure.ma.get(60) is not None and structure.ma[60] > structure.ma[120]:
            reasons.append("MA60 > MA120，长期结构成立")
        reasons.append(f"收盘站上 MA20 已 {structure.trend_days} 个交易日")
        if pullback.depth_pct is not None:
            reasons.append(f"阶段回撤 {pullback.depth_pct:.1f}%（{pullback.pullback_days} 天）")
        if pullback.atr_multiple is not None:
            reasons.append(f"ATR 标准化回撤 {pullback.atr_multiple:.2f}×")
        if pullback.volume_shrink_ratio is not None:
            reasons.append(f"回调期量能 {pullback.volume_shrink_ratio:.2f}× 上涨期")
        if pullback.trough_vs_ma20_pct is not None:
            reasons.append(f"回调低点距 MA20 {explanation.pct(pullback.trough_vs_ma20_pct)}")
        if pullback.rebound_confirmed:
            reasons.append("已出现再次转强的量价确认")
        if rps60 is not None:
            reasons.append(f"RPS60 {rps60:.1f}")

    if exceptions:
        risks.append("例外标记：" + "；".join(exceptions))
    if (structure.distance_ma20_pct or 0) > 10:
        risks.append(f"距 MA20 乖离 {structure.distance_ma20_pct:.1f}%，追高风险")
    if rps60 is None:
        risks.append("RPS60 缺失，相对强度无法确认")

    return ModelBResult(
        code=code or bars.code,
        name=name,
        trade_date=target,
        eligible=eligible,
        score=score,
        parts=parts,
        tier=level,
        structure=structure,
        momentum=mom,
        volume=volume,
        pullback=pullback,
        trend_strength=trend_strength,
        exception_flags=tuple(exceptions),
        reasons=explanation.collect(tuple(reasons)),
        exclusions=explanation.collect(tuple(exclusions)),
        risks=explanation.collect(tuple(risks)),
        missing=explanation.collect(tuple(missing)),
    )
