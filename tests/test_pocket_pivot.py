"""口袋支点两个实验版本：判定口径与「不进生产注册表」的守卫。

数值都是构造出来的最小样本（不是行情），只钉住：
- 原版必须「当日量 > 前 10 日所有下跌日的最大量」且都在均线附近/上方；
- 通达信版必须「涨幅 >5% + 成交额 ≥ 前 10 日最高 + 站上 MA90」；
- 两者都**不在** `PATTERNS` 里（实验形态只给回测脚本用）。
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import draft_patterns as draft  # noqa: E402

from app.services.patterns import PATTERNS, Bars  # noqa: E402


def _bars(closes, volumes, amounts=None, pct_chg=None) -> Bars:
    close = np.array(closes, dtype=float)
    count = len(close)
    return Bars(
        dates=[date(2026, 1, 1) + timedelta(days=index) for index in range(count)],
        open=close.copy(),
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        volume=np.array(volumes, dtype=float),
        amount=np.array(amounts if amounts is not None else volumes, dtype=float),
        pct_chg=np.array(
            pct_chg if pct_chg is not None else np.zeros(count), dtype=float
        ),
    )


def _kacher_case(last_volume: float = 9000.0):
    """71 根：横盘，第 65 根小跌（量 500），最后一根放量上涨 2%。"""
    closes = [100.0] * 70 + [102.0]
    closes[65] = 99.9
    volumes = [1000.0] * 71
    volumes[65] = 500.0
    volumes[-1] = last_volume
    return _bars(closes, volumes)


def test_kacher_passes_on_volume_expansion():
    signal = draft.pocket_pivot_kacher(_kacher_case())
    assert signal is not None
    assert signal.pattern == "pocket_pivot_kacher"


def test_kacher_needs_volume_above_down_days():
    assert draft.pocket_pivot_kacher(_kacher_case(last_volume=400.0)) is None


def test_kacher_needs_enough_history():
    bars = _kacher_case()
    short = Bars(
        dates=bars.dates[-30:],
        open=bars.open[-30:],
        high=bars.high[-30:],
        low=bars.low[-30:],
        close=bars.close[-30:],
        volume=bars.volume[-30:],
        amount=bars.amount[-30:],
        pct_chg=bars.pct_chg[-30:],
    )
    assert draft.pocket_pivot_kacher(short) is None


def _ths_case(last_pct: float = 6.0, last_amount: float = 5000.0):
    closes = [100.0] * 99 + [106.0]
    amounts = [1000.0] * 100
    amounts[-1] = last_amount
    pct = [0.0] * 100
    pct[-1] = last_pct
    return _bars(closes, [1000.0] * 100, amounts=amounts, pct_chg=pct)


def test_ths_passes_on_amount_and_gain():
    signal = draft.pocket_pivot_ths(_ths_case())
    assert signal is not None
    assert signal.pattern == "pocket_pivot_ths"


def test_ths_needs_gain_above_five_percent():
    assert draft.pocket_pivot_ths(_ths_case(last_pct=4.0)) is None


def test_ths_needs_amount_above_prior_max():
    assert draft.pocket_pivot_ths(_ths_case(last_amount=900.0)) is None


def test_drafts_are_not_registered_in_production():
    production = {pattern.key for pattern in PATTERNS}
    for key in ("pocket_pivot_kacher", "pocket_pivot_ths"):
        assert key not in production
