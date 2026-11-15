"""趋势结构：均线族、斜率、排列、收敛、持续天数。

只描述结构，不打分——模型各自决定怎么用。所有字段在数据不足时为 `None`，
`missing` 里写明缺什么，调用方不得把 `None` 当成 0 或「健康」。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.patterns import Bars
from app.services.trend_engine import config as cfg_module
from app.services.trend_engine import indicators


@dataclass(frozen=True, slots=True)
class TrendStructure:
    close: float | None
    ma: dict[int, float | None] = field(default_factory=dict)
    slope: dict[int, float | None] = field(default_factory=dict)
    normalized_slope: dict[int, float | None] = field(default_factory=dict)
    alignment: str = "insufficient"
    #: MA5/10/20/60 的极差 / 均值（%），越小越粘合
    convergence_pct: float | None = None
    #: 连续收盘 > MA20 的交易日数
    trend_days: int = 0
    #: MA20 / MA60 方向：up / flat / down / None
    ma20_turn: str | None = None
    ma60_turn: str | None = None
    #: MA20 与 MA60 的距离（%）：(MA20 / MA60 − 1) × 100
    gap_ma20_ma60_pct: float | None = None
    #: 收盘相对 MA20 / MA60 的乖离（%）
    distance_ma20_pct: float | None = None
    distance_ma60_pct: float | None = None
    missing: tuple[str, ...] = ()

    @property
    def above_ma20(self) -> bool | None:
        if self.close is None or self.ma.get(20) is None:
            return None
        return self.close > self.ma[20]

    @property
    def above_ma60(self) -> bool | None:
        if self.close is None or self.ma.get(60) is None:
            return None
        return self.close > self.ma[60]


def _turn(slope: float | None, flat: float) -> str | None:
    if slope is None:
        return None
    if slope > flat:
        return "up"
    if slope < -flat:
        return "down"
    return "flat"


def _distance(price: float | None, ma: float | None) -> float | None:
    if price is None or ma is None or ma <= 0:
        return None
    return round((price / ma - 1) * 100, 2)


def evaluate(bars: Bars, config: cfg_module.IndicatorConfig | None = None) -> TrendStructure:
    """计算结构指标。`bars` 必须是**前复权**序列（`patterns.build_bars`）。"""
    config = config or cfg_module.IndicatorConfig()
    close = bars.close
    missing: list[str] = []
    if len(bars) == 0:
        return TrendStructure(None, missing=("没有日线",))

    price = float(close[-1])
    ma = {period: indicators.ma_last(close, period) for period in config.ma_periods}
    for period, value in ma.items():
        if value is None:
            missing.append(f"MA{period}")

    atr_pct_value = indicators.atr_pct(bars.high, bars.low, close, config.atr_window)
    slope = {
        period: indicators.slope_pct(close, period, config.slope_days)
        for period in (5, 10, 20, 60)
    }
    normalized = {
        period: indicators.normalized_slope(
            close, period, config.slope_long_days, atr_pct_value
        )
        for period in (20, 60)
    }

    ma20, ma60 = ma.get(20), ma.get(60)
    gap = None
    if ma20 is not None and ma60 is not None and ma60 > 0:
        gap = round((ma20 / ma60 - 1) * 100, 2)

    structure = TrendStructure(
        close=round(price, 4),
        ma=ma,
        slope=slope,
        normalized_slope=normalized,
        alignment=indicators.ma_alignment(close, (5, 10, 20, 60)),
        convergence_pct=indicators.convergence_pct(close, (5, 10, 20, 60)),
        trend_days=indicators.trend_days_above(close, 20),
        ma20_turn=_turn(slope.get(20), config.slope_flat_pct),
        ma60_turn=_turn(slope.get(60), config.slope_flat_pct),
        gap_ma20_ma60_pct=gap,
        distance_ma20_pct=_distance(price, ma20),
        distance_ma60_pct=_distance(price, ma60),
        missing=tuple(missing),
    )
    return structure
