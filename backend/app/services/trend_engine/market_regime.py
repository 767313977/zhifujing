"""市场环境：BULL / SIDEWAYS / BEAR / EXTREME_RISK / UNKNOWN。

数据源优先级与降级规则：

1. `index_daily`（默认上证）——**必需**；没有指数就没有环境结论；
2. `breadth_daily`（站上 MA20/MA60 占比）——缺失时降低置信度，不做猜测；
3. `market_sentiment`（涨跌家数、跌停数）——极端风险判据；
4. `fear_greed_daily`（跌停强度、波动）——极端风险判据。

硬规则：

- 至少 `min_inputs` 个来源可用才给结论，否则 `UNKNOWN`；
- 每个来源都返回**实际数据日期**，页面必须显示 `actual_data_date`，不允许把旧日期当成当天；
- 缺数据的来源写进 `missing`，`confidence` 降级；**不得伪造** BULL/BEAR。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BreadthDaily, FearGreedDaily, IndexDaily, MarketSentiment
from app.services.trend_engine import config as cfg_module
from app.services.trend_engine import indicators

BULL = "BULL"
SIDEWAYS = "SIDEWAYS"
BEAR = "BEAR"
EXTREME_RISK = "EXTREME_RISK"
UNKNOWN = "UNKNOWN"

CALC_VERSION = "market-regime-1.0.0"

#: 环境 → 模型权重系数（由调用方决定是否乘到排序分上；模型原始分不变）
MODEL_WEIGHT_FACTOR = {
    BULL: 1.0,
    SIDEWAYS: 0.95,
    BEAR: 0.85,
    EXTREME_RISK: 0.0,
    UNKNOWN: 0.9,
}


@dataclass(frozen=True, slots=True)
class MarketRegime:
    regime: str
    trade_date: date | None
    confidence: str
    inputs: dict = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    calc_version: str = CALC_VERSION

    @property
    def model_weight_factor(self) -> float:
        return MODEL_WEIGHT_FACTOR.get(self.regime, 0.9)


def classify(inputs: dict, config: cfg_module.MarketConfig | None = None) -> tuple[str, list[str]]:
    """纯分类函数。`inputs` 只含已核实的数值（None 表示该来源缺失）。"""
    config = config or cfg_module.MarketConfig()
    reasons: list[str] = []

    close = inputs.get("close")
    ma20 = inputs.get("ma20")
    ma60 = inputs.get("ma60")
    slope20 = inputs.get("ma20_slope")
    slope60 = inputs.get("ma60_slope")
    above_ma20 = inputs.get("above_ma20")
    limit_down = inputs.get("limit_down")
    limit_strength = inputs.get("limit_strength")
    volatility = inputs.get("volatility")

    # ---- 极端风险优先 ----
    if limit_down is not None and limit_down >= config.extreme_limit_down:
        return EXTREME_RISK, [f"跌停家数 {limit_down} ≥ {config.extreme_limit_down}"]
    if above_ma20 is not None and above_ma20 < 15:
        return EXTREME_RISK, [f"站上 MA20 占比仅 {above_ma20:.0f}%"]
    if limit_strength is not None and limit_strength <= -60:
        return EXTREME_RISK, [f"涨跌停强度 {limit_strength:.0f}（跌停占优）"]
    if volatility is not None and volatility >= config.extreme_qvix_pct:
        return EXTREME_RISK, [f"波动率 {volatility:.1f}% ≥ {config.extreme_qvix_pct:g}%"]

    if close is None or ma20 is None or ma60 is None:
        return UNKNOWN, ["指数或均线不足"]

    if close < ma60 and (slope60 is None or slope60 < 0):
        reasons.append(f"指数 {close:.2f} 低于 MA60 {ma60:.2f} 且 MA60 向下")
        return BEAR, reasons

    bullish = close > ma20 and ma20 > ma60 and (slope20 is None or slope20 >= 0)
    if bullish:
        # 广度否决：指数强但个股普跌，不能算 BULL
        if above_ma20 is not None and above_ma20 < 30:
            reasons.append(f"指数在 MA20 上方，但站上 MA20 个股仅 {above_ma20:.0f}%")
            return SIDEWAYS, reasons
        reasons.append(f"指数 {close:.2f} > MA20 {ma20:.2f} > MA60 {ma60:.2f}")
        return BULL, reasons

    reasons.append("指数与均线未形成一致方向")
    return SIDEWAYS, reasons


def _latest(session: Session, model, day: date, *, order_field="trade_date"):
    return session.scalar(
        select(model)
        .where(getattr(model, order_field) <= day)
        .order_by(getattr(model, order_field).desc())
        .limit(1)
    )


def evaluate(
    session: Session,
    *,
    day: date | None = None,
    config: cfg_module.MarketConfig | None = None,
) -> MarketRegime:
    config = config or cfg_module.MarketConfig()
    target = day or session.scalar(select(IndexDaily.trade_date).order_by(IndexDaily.trade_date.desc()).limit(1))
    if target is None:
        return MarketRegime(UNKNOWN, None, "none", {}, ("没有指数日线",), ("index_daily",))

    rows = session.execute(
        select(IndexDaily.trade_date, IndexDaily.close)
        .where(IndexDaily.code == config.index_code, IndexDaily.trade_date <= target)
        .order_by(IndexDaily.trade_date)
    ).all()
    closes = [(when, float(close)) for when, close in rows if close is not None]
    if len(closes) < config.ma_slow + 5:
        return MarketRegime(
            UNKNOWN,
            target,
            "none",
            {"index_date": str(closes[-1][0]) if closes else None},
            ("指数历史不足 60 根，无法判断",),
            ("index_daily",),
        )

    values = np.asarray([value for _when, value in closes])
    dates = [when for when, _value in closes]
    index_date = dates[-1]
    ma20 = indicators.ma_last(values, config.ma_fast)
    ma60 = indicators.ma_last(values, config.ma_slow)
    slope20 = indicators.slope_pct(values, config.ma_fast, 5)
    slope60 = indicators.slope_pct(values, config.ma_slow, 5)

    breadth = _latest(session, BreadthDaily, target)
    sentiment = _latest(session, MarketSentiment, target)
    fear = _latest(session, FearGreedDaily, target)

    inputs: dict = {
        "close": float(values[-1]),
        "ma20": ma20,
        "ma60": ma60,
        "ma20_slope": slope20,
        "ma60_slope": slope60,
        "above_ma20": None if breadth is None else breadth.above_ma20,
        "above_ma60": None if breadth is None else breadth.above_ma60,
        "limit_down": None if sentiment is None else sentiment.limit_down_count,
        "limit_up": None if sentiment is None else sentiment.limit_up_count,
        "limit_strength": None if fear is None else fear.limit_strength,
        "volatility": None if fear is None else fear.volatility,
        "index_date": str(index_date),
        "breadth_date": None if breadth is None else str(breadth.trade_date),
        "sentiment_date": None if sentiment is None else str(sentiment.trade_date),
        "fear_greed_date": None if fear is None else str(fear.trade_date),
    }

    available = 1  # 指数
    missing: list[str] = []
    for name, value in (
        ("breadth_daily", breadth),
        ("market_sentiment", sentiment),
        ("fear_greed_daily", fear),
    ):
        if value is None:
            missing.append(name)
        else:
            available += 1
    if available < config.min_inputs:
        return MarketRegime(
            UNKNOWN,
            target,
            "none",
            inputs,
            (f"可用来源仅 {available} 个（要求 ≥{config.min_inputs}）",),
            tuple(missing),
        )

    regime, reasons = classify(inputs, config)
    same_day = all(
        inputs.get(key) == str(target)
        for key in ("breadth_date", "sentiment_date", "fear_greed_date")
        if inputs.get(key) is not None
    )
    if same_day and not missing and index_date == target:
        confidence = "high"
    elif index_date == target:
        confidence = "medium"
    else:
        confidence = "low"
        reasons.append(
            f"指数数据日 {index_date} 不是目标日 {target}，环境结论只代表 {index_date}"
        )

    return MarketRegime(
        regime=regime,
        trade_date=target,
        confidence=confidence,
        inputs=inputs,
        reasons=tuple(reasons),
        missing=tuple(missing),
    )
