"""Model A（趋势启动）单测：平台高点排除当日、放量分母、排除规则与权重可配置。"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from app.services.patterns import Bars
from app.services.trend_engine import breakout_model, config, screening


def _bars(
    *,
    breakout: bool = True,
    today_amount: float = 3e8,
    history_amount: float = 1e8,
    volume_spike: float = 3.0,
    drop_platform_days: int = 0,
    today_high_extra: float = 1.003,
    today_open_ratio: float = 0.99,
    today_low_ratio: float = 0.985,
) -> tuple[Bars, list[date]]:
    """80 天上涨 + 59 天窄平台 + 1 天突破。返回 (Bars, 交易日轴)。"""
    closes: list[float] = []
    price = 10.0
    for _ in range(80):
        price *= 1.005
        closes.append(price)
    for _ in range(59):
        price *= 1.001
        closes.append(price)
    platform_high = max(closes[-30:])
    closes.append(platform_high * (1.02 if breakout else 0.99))

    highs = [value * 1.01 for value in closes[:-1]]
    lows = [value * 0.99 for value in closes[:-1]]
    opens = [value * 0.998 for value in closes[:-1]]
    today_close = closes[-1]
    highs.append(today_close * today_high_extra)
    lows.append(today_close * today_low_ratio)
    opens.append(today_close * today_open_ratio)

    volumes = [1e6] * (len(closes) - 1) + [1e6 * volume_spike]
    amounts = [history_amount] * (len(closes) - 1) + [today_amount]
    pct = np.zeros(len(closes))
    for index in range(1, len(closes)):
        pct[index] = (closes[index] / closes[index - 1] - 1) * 100

    dates = [date(2025, 1, 1) + timedelta(days=index) for index in range(len(closes))]
    if drop_platform_days:
        # 删掉平台期最后 N 天（模拟停牌/漏采），保留今天
        drop = set(range(len(dates) - 1 - drop_platform_days, len(dates) - 1))
        keep = [index for index in range(len(dates)) if index not in drop]
        dates = [dates[index] for index in keep]
        highs = [highs[index] for index in keep]
        lows = [lows[index] for index in keep]
        opens = [opens[index] for index in keep]
        closes = [closes[index] for index in keep]
        volumes = [volumes[index] for index in keep]
        amounts = [amounts[index] for index in keep]
        pct = pct[keep]

    bars = Bars(
        dates=dates,
        open=np.asarray(opens),
        high=np.asarray(highs),
        low=np.asarray(lows),
        close=np.asarray(closes),
        volume=np.asarray(volumes),
        amount=np.asarray(amounts),
        pct_chg=np.asarray(pct),
        code="300001",
    )
    return bars, dates


RPS = {"rps20": 82.0, "rps60": 78.0, "rps120": 70.0, "rps250": 65.0}


def test_breakout_is_eligible() -> None:
    bars, dates = _bars()
    result = breakout_model.evaluate(
        bars, code="300001", name="测试股", day=dates[-1], rps_values=RPS
    )
    assert result.eligible is True, result.exclusions
    assert result.breakout_pct is not None and result.breakout_pct > 0
    assert result.volume_confirm is True
    assert result.score > 0
    assert set(result.parts) == {"structure", "breakout", "volume", "rps", "sector"}


def test_platform_high_excludes_today_even_if_today_spikes() -> None:
    bars, dates = _bars(today_high_extra=5.0)  # 今日最高价远高于平台
    result = breakout_model.evaluate(bars, day=dates[-1], rps_values=RPS)
    assert result.platform_high == pytest.approx(float(np.max(bars.high[-31:-1])))
    assert result.platform_high < float(bars.high[-1])


def test_no_breakout_is_excluded() -> None:
    bars, dates = _bars(breakout=False)
    result = breakout_model.evaluate(bars, day=dates[-1], rps_values=RPS)
    assert result.eligible is False
    assert result.score == 0.0
    assert any("未突破平台高点" in item for item in result.exclusions)


def test_volume_denominator_excludes_today() -> None:
    bars, dates = _bars(volume_spike=1.0, today_amount=1e8)
    result = breakout_model.evaluate(bars, day=dates[-1], rps_values=RPS)
    assert result.volume is not None
    assert result.volume.amount_ratio20 == pytest.approx(1.0, abs=0.01)
    assert result.eligible is False
    assert any("未放量" in item for item in result.exclusions)


def test_single_day_amount_spike_does_not_inflate_score() -> None:
    normal, dates = _bars(today_amount=3e8)
    abnormal, _ = _bars(today_amount=9e8)
    left = breakout_model.evaluate(normal, day=dates[-1], rps_values=RPS)
    right = breakout_model.evaluate(abnormal, day=dates[-1], rps_values=RPS)
    assert right.volume is not None and right.volume.abnormal_amount is True
    assert right.parts["volume"] <= left.parts["volume"] + 1e-9
    assert any("异常放大" in item for item in right.risks)


def test_st_and_suspension_are_excluded() -> None:
    bars, dates = _bars()
    st = breakout_model.evaluate(bars, name="*ST测试", day=dates[-1], rps_values=RPS)
    assert any("ST" in item for item in st.exclusions)

    stale = breakout_model.evaluate(bars, day=dates[-1] + timedelta(days=1), rps_values=RPS)
    assert stale.eligible is False
    assert any("不是目标日" in item for item in stale.exclusions)


def test_liquidity_filter_is_configurable() -> None:
    bars, dates = _bars(history_amount=1e7, today_amount=3e7)
    strict = breakout_model.evaluate(bars, day=dates[-1], rps_values=RPS)
    assert any("流动性不足" in item for item in strict.exclusions)
    loose = breakout_model.evaluate(
        bars,
        day=dates[-1],
        rps_values=RPS,
        config=config.model_a_config(min_amount20=5e6),
    )
    assert loose.eligible is True


def test_upper_shadow_excludes() -> None:
    bars, dates = _bars(today_high_extra=1.08, today_open_ratio=0.99, today_low_ratio=0.985)
    result = breakout_model.evaluate(bars, day=dates[-1], rps_values=RPS)
    assert result.volume is not None
    assert result.volume.upper_shadow_pct is not None
    assert result.volume.upper_shadow_pct > 50
    assert any("长上影" in item for item in result.exclusions)


def test_platform_suspension_coverage_excludes() -> None:
    bars, _dates = _bars(drop_platform_days=10)
    full_calendar = [date(2025, 1, 1) + timedelta(days=index) for index in range(140)]
    result = breakout_model.evaluate(
        bars, day=full_calendar[-1], rps_values=RPS, trade_days=full_calendar
    )
    assert result.eligible is False
    assert any("停牌过多" in item for item in result.exclusions)


def test_sector_and_rps_weights_are_configurable() -> None:
    bars, dates = _bars()
    base = breakout_model.evaluate(bars, day=dates[-1], rps_values=RPS)
    with_sector = breakout_model.evaluate(
        bars, day=dates[-1], rps_values=RPS, sector_score=95.0
    )
    assert with_sector.score == pytest.approx(base.score)  # 默认 0 分，不影响总分

    sector_on = breakout_model.evaluate(
        bars,
        day=dates[-1],
        rps_values=RPS,
        sector_score=95.0,
        config=config.model_a_config(sector_points=5.0),
    )
    assert sector_on.score > base.score
    assert screening.weights_for(config.model_a_config(sector_points=5.0))["sector"] == 5.0

    rps_off = breakout_model.evaluate(
        bars, day=dates[-1], rps_values=RPS, config=config.model_a_config(rps_points=0.0)
    )
    assert base.score - rps_off.score <= 3.0 + 1e-9


def test_missing_rps_is_none_not_zero_part() -> None:
    bars, dates = _bars()
    result = breakout_model.evaluate(bars, day=dates[-1])
    assert result.parts["rps"] is None
    assert "rps" in result.missing
    assert any("RPS" in item for item in result.risks)


def test_top_does_not_pad() -> None:
    bars, dates = _bars()
    good = breakout_model.evaluate(bars, day=dates[-1], rps_values=RPS)
    rows = screening.top([good], limit=20)
    assert len(rows) == 1
    assert screening.start_pool(good.score, good.eligible) in {screening.START_POOL, screening.WATCH_POOL}
