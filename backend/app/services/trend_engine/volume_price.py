"""量价结构：放量 / 缩量 / 量价背离 / 成交额持续性。

口径（全项目统一，改这里必须升 `CALC_VERSION`）：

- **成交量比** = 当日成交量 / 前 5 日均量，分母**不含当日**；
- **成交额比** = 当日成交额 / 前 20 日均额，分母**不含当日**；
- 成交额单位是**元**（`stock_daily.amount`），不换算成万元；
- 单日成交额放大**不等于**资金净流入 —— 本模块不产出任何「净流入」字段；
- 单日异常放量（默认 > 8 倍）只标记、不加分，避免一笔大宗刷出高分。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.patterns import Bars
from app.services.trend_engine import config as cfg_module
from app.services.trend_engine import indicators


@dataclass(frozen=True, slots=True)
class VolumePriceState:
    vol_ratio5: float | None = None
    amount_ratio5: float | None = None
    amount_ratio20: float | None = None
    #: 上涨日成交量 / 下跌日成交量（最近 20 日）
    up_down_volume_ratio20: float | None = None
    #: 最近 5 日均额 / 最近 20 日均额（都含当日），衡量量能是否持续
    amount_trend_ratio: float | None = None
    #: 当日收盘在振幅中的位置（0~1）
    close_position: float | None = None
    upper_shadow_pct: float | None = None
    #: `price_up_volume_down` / `price_down_volume_up` / None
    divergence: str | None = None
    #: 单日成交额是否异常放大（> config.max_breakout_amount_ratio）
    abnormal_amount: bool = False
    missing: tuple[str, ...] = ()


def evaluate(
    bars: Bars,
    config: cfg_module.ModelAConfig | cfg_module.ModelBConfig | None = None,
) -> VolumePriceState:
    config = config or cfg_module.ModelAConfig()
    indicator_cfg = config.indicator
    missing: list[str] = []

    vol_ratio5 = indicators.volume_ratio(bars.volume, indicator_cfg.volume_window)
    amount_ratio5 = indicators.amount_ratio(bars.amount, 5)
    amount_ratio20 = indicators.amount_ratio(bars.amount, indicator_cfg.amount_window)
    for name, value in (
        ("成交量比", vol_ratio5),
        ("5日成交额比", amount_ratio5),
        ("20日成交额比", amount_ratio20),
    ):
        if value is None:
            missing.append(name)

    up_down = indicators.up_down_volume_ratio(bars.close, bars.volume, 20)
    if up_down is None:
        missing.append("上涨/下跌量能比")

    amount_trend = None
    amount = bars.amount
    if len(amount) >= 20 and all(
        value is not None and value == value for value in amount[-20:]
    ):
        short = float(amount[-5:].mean())
        long = float(amount[-20:].mean())
        if long > 0:
            amount_trend = round(short / long, 2)

    ret5 = indicators.ret_pct(bars.pct_chg, 5)
    divergence = None
    if ret5 is not None and amount_trend is not None:
        if ret5 > 0 and amount_trend < 0.7:
            divergence = "price_up_volume_down"
        elif ret5 < 0 and amount_trend > 1.5:
            divergence = "price_down_volume_up"

    max_ratio = getattr(config, "max_breakout_amount_ratio", 8.0)
    abnormal = bool(amount_ratio20 is not None and amount_ratio20 > max_ratio)

    return VolumePriceState(
        vol_ratio5=vol_ratio5,
        amount_ratio5=amount_ratio5,
        amount_ratio20=amount_ratio20,
        up_down_volume_ratio20=up_down,
        amount_trend_ratio=amount_trend,
        close_position=indicators.close_position(bars.high, bars.low, bars.close),
        upper_shadow_pct=indicators.upper_shadow_pct(
            bars.open, bars.high, bars.low, bars.close
        ),
        divergence=divergence,
        abnormal_amount=abnormal,
        missing=tuple(missing),
    )
