"""Model B（趋势延续）单测：回调结构、缩量、MA60 例外标记与分层。"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

import numpy as np

from app.services.patterns import Bars
from app.services.trend_engine import config, continuation_model, screening, trend_structure


def _b_bars(
    *,
    rise_days: int = 110,
    rise_pct: float = 0.006,
    pull_days: int = 7,
    pull_pct: float = -0.006,
    rebound_days: int = 35,
    rebound_pct: float = 0.005,
    pull_volume: float = 0.5,
) -> tuple[Bars, list[date]]:
    closes: list[float] = []
    volumes: list[float] = []
    price = 10.0
    for _ in range(rise_days):
        price *= 1 + rise_pct
        closes.append(price)
        volumes.append(1e6)
    for _ in range(pull_days):
        price *= 1 + pull_pct
        closes.append(price)
        volumes.append(1e6 * pull_volume)
    for _ in range(rebound_days):
        price *= 1 + rebound_pct
        closes.append(price)
        volumes.append(1e6 * pull_volume * 1.3)

    highs = [value * 1.01 for value in closes]
    lows = [value * 0.99 for value in closes]
    opens = [value * 0.998 for value in closes]
    amounts = [value * 100 for value in volumes]
    pct = np.zeros(len(closes))
    for index in range(1, len(closes)):
        pct[index] = (closes[index] / closes[index - 1] - 1) * 100
    dates = [date(2024, 1, 1) + timedelta(days=index) for index in range(len(closes))]
    bars = Bars(
        dates=dates,
        open=np.asarray(opens),
        high=np.asarray(highs),
        low=np.asarray(lows),
        close=np.asarray(closes),
        volume=np.asarray(volumes),
        amount=np.asarray(amounts),
        pct_chg=np.asarray(pct),
        code="600001",
    )
    return bars, dates


RPS = {"rps20": 75.0, "rps60": 86.0, "rps120": 80.0, "rps250": 72.0}


def test_continuation_happy_path() -> None:
    bars, dates = _b_bars()
    result = continuation_model.evaluate(bars, day=dates[-1], rps_values=RPS)
    assert result.eligible is True, result.exclusions
    assert result.score > 0
    assert result.pullback is not None
    assert 3 <= (result.pullback.depth_pct or 0) <= 12
    assert result.pullback.atr_multiple is not None
    assert result.pullback.volume_shrink_ratio is not None and result.pullback.volume_shrink_ratio < 0.8
    assert result.trend_strength is not None and result.trend_strength > 0
    assert result.pullback.support_price is not None
    assert set(result.parts) == {"structure", "pullback", "volume", "rps", "sector"}


def test_no_pullback_is_excluded() -> None:
    bars, dates = _b_bars(pull_days=1, pull_pct=-0.002)
    result = continuation_model.evaluate(bars, day=dates[-1], rps_values=RPS)
    assert result.eligible is False
    assert any("尚未形成有效回调" in item for item in result.exclusions)


def test_deep_pullback_is_excluded() -> None:
    bars, dates = _b_bars(pull_days=12, pull_pct=-0.015)
    result = continuation_model.evaluate(bars, day=dates[-1], rps_values=RPS)
    assert result.eligible is False
    assert any("回调过深" in item for item in result.exclusions)


def test_ma20_below_ma60_is_excluded() -> None:
    closes: list[float] = []
    price = 20.0
    for _ in range(120):
        price *= 0.997
        closes.append(price)
    for _ in range(20):
        price *= 1.002
        closes.append(price)
    pct = np.zeros(len(closes))
    for index in range(1, len(closes)):
        pct[index] = (closes[index] / closes[index - 1] - 1) * 100
    bars = Bars(
        dates=[date(2024, 1, 1) + timedelta(days=index) for index in range(len(closes))],
        open=np.asarray(closes),
        high=np.asarray([value * 1.01 for value in closes]),
        low=np.asarray([value * 0.99 for value in closes]),
        close=np.asarray(closes),
        volume=np.full(len(closes), 1e6),
        amount=np.full(len(closes), 1e8),
        pct_chg=pct,
    )
    result = continuation_model.evaluate(bars, day=bars.dates[-1], rps_values=RPS)
    assert result.eligible is False
    assert any("MA20 未高于 MA60" in item or "MA20 向下" in item for item in result.exclusions)


def _fake_structure(**overrides) -> trend_structure.TrendStructure:
    base = dict(
        close=20.0,
        ma={5: 19.9, 10: 19.7, 20: 19.5, 30: 19.0, 60: 18.0, 120: 17.0, 250: 15.0},
        slope={5: 0.5, 10: 0.4, 20: 0.3, 60: 0.1},
        normalized_slope={20: 0.2, 60: 0.1},
        alignment="bull",
        convergence_pct=8.0,
        trend_days=30,
        ma20_turn="up",
        ma60_turn="down",
        gap_ma20_ma60_pct=8.3,
        distance_ma20_pct=2.5,
        distance_ma60_pct=11.1,
        missing=(),
    )
    base.update(overrides)
    return trend_structure.TrendStructure(**base)


def test_ma60_down_is_flagged_not_hidden() -> None:
    bars, dates = _b_bars()
    fake = _fake_structure()
    with patch.object(continuation_model.trend_structure, "evaluate", return_value=fake):
        allowed = continuation_model.evaluate(bars, day=dates[-1], rps_values=RPS)
    assert any("MA60" in item for item in allowed.exception_flags)
    assert any("例外标记" in item for item in allowed.risks)

    with patch.object(continuation_model.trend_structure, "evaluate", return_value=fake):
        strict = continuation_model.evaluate(
            bars,
            day=dates[-1],
            rps_values=RPS,
            config=config.model_b_config(allow_ma60_down_exception=False),
        )
    assert any("MA60 明显向下" in item for item in strict.exclusions)


def test_volume_expansion_in_pullback_lowers_score() -> None:
    quiet, dates = _b_bars(pull_volume=0.5)
    noisy, _ = _b_bars(pull_volume=1.2)
    left = continuation_model.evaluate(quiet, day=dates[-1], rps_values=RPS)
    right = continuation_model.evaluate(noisy, day=dates[-1], rps_values=RPS)
    assert (right.parts["pullback"] or 0) < (left.parts["pullback"] or 0)
    assert any("未缩量" in item for item in right.risks)


def test_stale_day_and_missing_rps() -> None:
    bars, dates = _b_bars()
    stale = continuation_model.evaluate(bars, day=dates[-1] + timedelta(days=1), rps_values=RPS)
    assert stale.eligible is False
    assert any("不是目标日" in item for item in stale.exclusions)

    fresh = continuation_model.evaluate(bars, day=dates[-1])
    assert fresh.parts["rps"] is None
    assert "rps" in fresh.missing


def test_continuation_pool_thresholds() -> None:
    assert screening.continuation_pool(90.0, True) == screening.CONTINUATION_POOL
    assert screening.continuation_pool(80.0, True) == screening.CONTINUATION_POOL
    assert screening.continuation_pool(70.0, True) == screening.WATCH_POOL
    assert screening.continuation_pool(70.0, False) is None
    assert screening.risk_pool("WARNING") == screening.WARNING_POOL
    assert screening.risk_pool("BROKEN") == screening.BROKEN_POOL
    assert screening.risk_pool("NORMAL") is None
