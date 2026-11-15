"""M5 风险引擎：NORMAL / WARNING / BROKEN 的多条件确认与模型差异。"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from app.services.patterns import Bars
from app.services.trend_engine import risk_engine


def _bars(closes: list[float], *, pct_override: list[float] | None = None) -> Bars:
    close = np.asarray(closes, dtype=float)
    pct = np.zeros(len(closes))
    for index in range(1, len(closes)):
        pct[index] = (close[index] / close[index - 1] - 1) * 100
    if pct_override is not None:
        pct = np.asarray(pct_override, dtype=float)
    return Bars(
        dates=[date(2024, 1, 1) + timedelta(days=index) for index in range(len(closes))],
        open=close,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        volume=np.full(len(closes), 1e6),
        amount=np.full(len(closes), 1e8),
        pct_chg=pct,
    )


def _steady(days: int = 200, step: float = 0.005) -> list[float]:
    price = 10.0
    out = []
    for _ in range(days):
        price *= 1 + step
        out.append(price)
    return out


def test_steady_uptrend_is_normal() -> None:
    bars = _bars(_steady())
    result = risk_engine.evaluate(bars, model="A", include_since=False)
    assert result.state == risk_engine.NORMAL
    assert result.reduce_attention is False
    assert result.severity == 0


def test_single_close_below_ma20_is_warning_not_broken() -> None:
    closes = _steady() + [_steady()[-1] * 0.94]
    bars = _bars(closes)
    result = risk_engine.evaluate(bars, model="A", include_since=False)
    assert result.state == risk_engine.WARNING
    assert result.conditions["close_below_ma20"] is True


def test_sharp_breakdown_is_broken() -> None:
    closes = _steady()
    price = closes[-1]
    for _ in range(6):
        price *= 0.97
        closes.append(price)
    bars = _bars(closes)
    fast = risk_engine.evaluate(bars, model="A", include_since=False)
    assert fast.state == risk_engine.BROKEN
    assert fast.severity == 2
    assert fast.failure_price is not None
    assert fast.atr_stop_price is not None
    assert len([name for name, value in fast.conditions.items() if value]) >= 2


def test_model_a_is_more_sensitive_than_model_b() -> None:
    closes = _steady()
    price = closes[-1]
    for _ in range(3):
        price *= 0.975
        closes.append(price)
    bars = _bars(closes)
    fast = risk_engine.evaluate(bars, model="A", include_since=False)
    slow = risk_engine.evaluate(bars, model="B", include_since=False)
    assert slow.state != risk_engine.BROKEN
    assert (fast.severity or 0) >= (slow.severity or 0)


def test_unconfirmed_ma60_break_is_warning_for_model_b() -> None:
    closes = _steady()
    closes.append(closes[-1] * 0.88)  # 单日大阴线，MA60 可能刚破
    bars = _bars(closes)
    result = risk_engine.evaluate(bars, model="B", include_since=False)
    assert result.state != risk_engine.BROKEN


def test_insufficient_data_is_unknown_not_normal() -> None:
    bars = _bars([10.0] * 50)
    result = risk_engine.evaluate(bars, model="A")
    assert result.state == risk_engine.UNKNOWN
    assert result.reduce_attention is True
    assert any("不足" in item for item in result.missing)


def test_stale_day_is_unknown() -> None:
    bars = _bars(_steady())
    result = risk_engine.evaluate(
        bars, model="A", day=bars.dates[-1] + timedelta(days=1)
    )
    assert result.state == risk_engine.UNKNOWN


def test_review_date_uses_trading_day_axis() -> None:
    bars = _bars(_steady())
    calendar = list(bars.dates) + [
        bars.dates[-1] + timedelta(days=index) for index in range(1, 11)
    ]
    result = risk_engine.evaluate(
        bars, model="A", trade_days=calendar, include_since=False
    )
    assert result.review_date == bars.dates[-1] + timedelta(days=5)


def test_since_tracks_state_start() -> None:
    closes = _steady() + [_steady()[-1] * 0.94]
    bars = _bars(closes)
    result = risk_engine.evaluate(bars, model="A", include_since=True)
    assert result.state == risk_engine.WARNING
    assert result.since is not None
    assert result.since >= bars.dates[-2]
