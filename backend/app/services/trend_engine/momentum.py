"""动量与相对强度：区间收益（复用 `rps.ret`）+ RPS 读数 + RPS 改善速度。

⚠️ 这里**不重算 RPS**：RPS 是横截面指标，必须读 `rps_daily`（由 `scan_rps` 统一计算）。
本模块只接收调用方传入的当日分位与历史分位序列，做「是否改善」的纯计算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Mapping, Sequence

from app.services.patterns import Bars
from app.services.trend_engine import config as cfg_module
from app.services.trend_engine import indicators

RPS_KEYS = ("rps20", "rps60", "rps120", "rps250")


@dataclass(frozen=True, slots=True)
class MomentumState:
    #: 窗口 → 复利涨幅（%）；缺窗口记 None
    ret: dict[int, float | None] = field(default_factory=dict)
    #: 当日 RPS 分位（0~100）；调用方没给或当日无行时为 None
    rps: dict[str, float | None] = field(default_factory=dict)
    #: RPS20 相对 `rps_drop_days` 个交易日前的变化（分位点）
    rps20_improve: float | None = None
    #: RPS60 相对 `rps_drop_days` 个交易日前的变化（分位点）
    rps60_improve: float | None = None
    #: RPS20 连续改善的交易日数（0 = 没有；None = 缺历史）
    rps20_consecutive_up: int | None = None
    missing: tuple[str, ...] = ()

    @property
    def rps20(self) -> float | None:
        return self.rps.get("rps20")

    @property
    def rps60(self) -> float | None:
        return self.rps.get("rps60")


def _history_improve(
    history: Sequence[tuple[date, Mapping[str, float | None]]] | None,
    key: str,
    days: int,
) -> float | None:
    if not history or len(history) <= days:
        return None
    latest = history[-1][1].get(key)
    base = history[-1 - days][1].get(key)
    if latest is None or base is None:
        return None
    return round(float(latest) - float(base), 1)


def _consecutive_up(
    history: Sequence[tuple[date, Mapping[str, float | None]]] | None,
    key: str,
    max_days: int = 10,
) -> int | None:
    """从最新往前数，RPS 连续上升的天数（最多 `max_days`）。"""
    if not history or len(history) < 2:
        return None
    values = [item.get(key) for _day, item in history[-max_days - 1 :]]
    if any(value is None for value in values):
        return None
    count = 0
    for index in range(len(values) - 1, 0, -1):
        if values[index] > values[index - 1]:
            count += 1
        else:
            break
    return count


def evaluate(
    bars: Bars,
    rps_values: Mapping[str, float | None] | None = None,
    *,
    rps_history: Sequence[tuple[date, Mapping[str, float | None]]] | None = None,
    config: cfg_module.IndicatorConfig | None = None,
    improve_days: int = 5,
) -> MomentumState:
    config = config or cfg_module.IndicatorConfig()
    missing: list[str] = []
    ret = {
        window: indicators.ret_pct(bars.pct_chg, window)
        for window in config.ret_windows
    }
    for window, value in ret.items():
        if value is None:
            missing.append(f"RET{window}")

    rps_map: dict[str, float | None] = {}
    for key in RPS_KEYS:
        value = None if rps_values is None else rps_values.get(key)
        rps_map[key] = None if value is None else round(float(value), 1)
        if rps_map[key] is None:
            missing.append(key.upper())

    return MomentumState(
        ret=ret,
        rps=rps_map,
        rps20_improve=_history_improve(rps_history, "rps20", improve_days),
        rps60_improve=_history_improve(rps_history, "rps60", improve_days),
        rps20_consecutive_up=_consecutive_up(rps_history, "rps20"),
        missing=tuple(missing),
    )
