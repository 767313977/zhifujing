"""趋势风险引擎：NORMAL / WARNING / BROKEN（数据不足时 UNKNOWN）。

设计原则：

- **BROKEN 必须多条件确认**：单日跌破 MA60 只升 WARNING，要求「连续收盘确认 +
  关键结构低点 / 均线结构 / 异常跳空」里至少 2 条同时成立；
- Model A（启动型）与 Model B（成熟趋势）用不同的失效锚：
  A 看 MA20 / 平台低点（趋势年轻，破坏更快），B 看 MA60 / 中期结构低点；
- 输出「失效参考价 / ATR 风险价 / 结构低点 / 复核条件 / 复核日期」，
  但这些只是研究提醒，**不是交易指令**，也不代表实际成交价。

数据不足（日线不够 / 目标日不匹配）返回 `UNKNOWN` —— 未知不是低风险。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np

from app.services.patterns import Bars
from app.services.trend_engine import config as cfg_module
from app.services.trend_engine import explanation, indicators, trend_structure

NORMAL = "NORMAL"
WARNING = "WARNING"
BROKEN = "BROKEN"
UNKNOWN = "UNKNOWN"

CALC_VERSION = "risk-engine-1.0.0"


@dataclass(frozen=True, slots=True)
class RiskState:
    state: str
    model: str
    trade_date: date | None
    since: date | None = None
    severity: int | None = None
    failure_price: float | None = None
    atr_stop_price: float | None = None
    structure_low: float | None = None
    review_date: date | None = None
    reduce_attention: bool = False
    conditions: dict = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    calc_version: str = CALC_VERSION


def _consecutive_below(close: np.ndarray, level: float, days: int) -> int:
    """从最新往前数，收盘连续低于 `level` 的天数（最多 `days`）。"""
    count = 0
    for index in range(close.size - 1, max(close.size - 1 - days, -1), -1):
        if np.isfinite(close[index]) and close[index] < level:
            count += 1
        else:
            break
    return count


def _structure_low(bars: Bars, days: int) -> float | None:
    """结构低点：不含当日的最低**收盘**（用 low 会把日内插针算进去）。"""
    if len(bars.close) < days + 1:
        return None
    chunk = bars.close[-days - 1 : -1]
    if chunk.size == 0 or not np.all(np.isfinite(chunk)):
        return None
    return round(float(np.min(chunk)), 4)


def _conditions(
    bars: Bars,
    model: str,
    structure: trend_structure.TrendStructure,
    config: cfg_module.RiskConfig,
) -> tuple[dict, dict]:
    """返回 (条件字典, 关键价位)。每个条件都是 bool 或 None（无法判断）。"""
    close = bars.close
    last = float(close[-1])
    low = _structure_low(bars, config.structure_low_days)
    ma20, ma60 = structure.ma.get(20), structure.ma.get(60)
    below_ma20 = None if ma20 is None else last < ma20 * (1 - config.ma20_break_pct / 100)
    below_ma60 = None if ma60 is None else last < ma60 * (1 - config.ma60_break_pct / 100)
    confirm20 = None if ma20 is None else _consecutive_below(close, ma20, config.confirm_days) >= config.confirm_days
    confirm60 = None if ma60 is None else _consecutive_below(close, ma60, config.confirm_days) >= config.confirm_days
    below_low = None if low is None else last < low
    ma20_below_60 = None
    if ma20 is not None and ma60 is not None:
        ma20_below_60 = ma20 < ma60
    gap_down = bool(float(bars.pct_chg[-1]) <= -5.0) if len(bars.pct_chg) else None

    if model == "A":
        conditions = {
            "close_below_ma20": below_ma20,
            "ma20_below_ma60": ma20_below_60,
            "close_below_structure_low": below_low,
            "confirm_below_ma20": confirm20,
            "gap_down": gap_down,
        }
        failure = min([value for value in (low, ma20) if value is not None], default=None)
    else:
        conditions = {
            "close_below_ma60": below_ma60,
            "confirm_below_ma60": confirm60,
            "close_below_structure_low": below_low,
            "ma20_below_ma60": ma20_below_60,
            "gap_down": gap_down,
        }
        failure = ma60
    levels = {"structure_low": low, "ma20": ma20, "ma60": ma60, "last": round(last, 4)}
    return conditions, {"failure_price": failure, **levels}


def _state_from(conditions: dict, config: cfg_module.RiskConfig) -> tuple[str, list[str]]:
    broken_hits = [name for name, value in conditions.items() if value is True and name in (
        "close_below_ma20", "ma20_below_ma60", "close_below_structure_low",
        "close_below_ma60", "confirm_below_ma20", "confirm_below_ma60", "gap_down",
    )]
    # 「连续 N 日低于均线」已经包含「当日低于均线」，不能重复计数同一事实
    if "confirm_below_ma20" in broken_hits and "close_below_ma20" in broken_hits:
        broken_hits.remove("close_below_ma20")
    if "confirm_below_ma60" in broken_hits and "close_below_ma60" in broken_hits:
        broken_hits.remove("close_below_ma60")
    # gap_down 单独不构成 BROKEN，必须与均线/结构条件叠加
    core = [name for name in broken_hits if name != "gap_down"]
    if len(core) >= config.broken_min_conditions:
        return BROKEN, core
    warning_hits = [name for name, value in conditions.items() if value is True]
    if warning_hits:
        return WARNING, warning_hits
    return NORMAL, []


def _state_at(bars: Bars, index: int, model: str, config: cfg_module.RiskConfig) -> str:
    """历史第 `index` 根（含）那天的状态，用于回溯「状态变化日期」。"""
    sliced = Bars(
        dates=bars.dates[: index + 1],
        open=bars.open[: index + 1],
        high=bars.high[: index + 1],
        low=bars.low[: index + 1],
        close=bars.close[: index + 1],
        volume=bars.volume[: index + 1],
        amount=bars.amount[: index + 1],
        pct_chg=bars.pct_chg[: index + 1],
        code=bars.code,
    )
    structure = trend_structure.evaluate(sliced)
    conditions, _levels = _conditions(sliced, model, structure, config)
    state, _reasons = _state_from(conditions, config)
    return state


def evaluate(
    bars: Bars,
    *,
    model: str = "A",
    day: date | None = None,
    config: cfg_module.RiskConfig | None = None,
    trade_days: list[date] | None = None,
    include_since: bool = True,
) -> RiskState:
    config = config or cfg_module.RiskConfig()
    model = model.upper()
    if model not in ("A", "B"):
        raise ValueError("model 只能是 A / B")
    target = day or (bars.dates[-1] if bars.dates else None)
    quality = indicators.evaluate_quality(bars, target, 70)
    if not quality.ok:
        return RiskState(
            state=UNKNOWN,
            model=model,
            trade_date=target,
            reduce_attention=True,
            conditions={},
            reasons=quality.missing,
            missing=quality.missing,
        )

    structure = trend_structure.evaluate(bars)
    conditions, levels = _conditions(bars, model, structure, config)
    state, hits = _state_from(conditions, config)

    since = None
    if include_since:
        since = bars.dates[-1]
        for index in range(len(bars) - 1, max(len(bars) - 1 - 60, -1), -1):
            if _state_at(bars, index, model, config) == state:
                since = bars.dates[index]
            else:
                break

    atr_value = indicators.atr_last(bars.high, bars.low, bars.close)
    atr_stop = None
    if atr_value is not None:
        atr_stop = round(float(bars.close[-1]) - config.atr_stop_multiple * atr_value, 2)

    review_date = None
    if trade_days and target in trade_days:
        position = trade_days.index(target)
        if position + config.review_days < len(trade_days):
            review_date = trade_days[position + config.review_days]

    reasons: list[str] = []
    if state == BROKEN:
        reasons.append("核心结构破坏：" + "、".join(hits))
    elif state == WARNING:
        reasons.append("趋势出现恶化信号：" + "、".join(hits))
    else:
        reasons.append("趋势结构暂未破坏")

    label = {
        "close_below_ma20": "收盘跌破 MA20",
        "close_below_ma60": "收盘跌破 MA60",
        "confirm_below_ma20": f"连续 {config.confirm_days} 日收盘低于 MA20",
        "confirm_below_ma60": f"连续 {config.confirm_days} 日收盘低于 MA60",
        "ma20_below_ma60": "MA20 跌破 MA60",
        "close_below_structure_low": f"跌破 {config.structure_low_days} 日结构低点",
        "gap_down": "异常跳空低开（单日 ≤ -5%）",
    }
    detail = tuple(label.get(name, name) for name in hits)
    if levels.get("failure_price") is not None:
        reasons.append(f"失效参考价 {levels['failure_price']:.2f}（前复权）")
    if atr_stop is not None:
        reasons.append(f"ATR{config.atr_stop_multiple:g} 倍风险参考价 {atr_stop:.2f}")

    return RiskState(
        state=state,
        model=model,
        trade_date=target,
        since=since,
        severity={NORMAL: 0, WARNING: 1, BROKEN: 2}.get(state),
        failure_price=levels.get("failure_price"),
        atr_stop_price=atr_stop,
        structure_low=levels.get("structure_low"),
        review_date=review_date,
        reduce_attention=state != NORMAL,
        conditions=conditions,
        reasons=explanation.collect(tuple(reasons), detail),
        missing=quality.missing,
    )
