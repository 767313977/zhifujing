"""形态跟踪的收益口径（纯函数，不碰数据库）。

覆盖三件事：
1. `_curves_from` 按**交易日**逐日复利（不是收盘价比值）；
2. `_stats` / `_pooled_stats` 的超额是「逐样本减掉自己那一天的全市场均值」；
3. 成绩单结论的阈值顺序（样本不足优先，噪声要求样本足够）。
"""

from datetime import date

import numpy as np
import pytest

from app.services.pattern_track import (
    _Matrix,
    _curves_from,
    _pooled_stats,
    _stats,
    _verdict,
)


def _matrix() -> tuple[_Matrix, list[date]]:
    days = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    index = {day: i for i, day in enumerate(days)}
    growth = np.array(
        [
            [1.0, 1.0],  # 命中日：入场
            [1.10, 1.02],  # 第 1 日：A +10%、B +2% → 全市场均值 +6%
            [0.90, 1.00],  # 第 2 日：A −10%、B 0%
            [1.00, 1.00],
        ]
    )
    matrix = _Matrix(
        calendar=days,
        index=index,
        date_pos=dict(index),
        code_pos={"A": 0, "B": 1},
        growth=growth,
        present=np.ones((4, 2), dtype=bool),
        complete=np.ones(4, dtype=bool),
        end_pos=3,
    )
    return matrix, days


def test_curves_compound_by_trading_day():
    matrix, days = _matrix()
    picks = {days[0]: [("A", "甲", 90.0), ("B", "乙", 80.0)]}

    curves = _curves_from(matrix, picks, track_days=2)
    values1, market1 = curves[days[0]][1]
    np.testing.assert_allclose(values1, [0.10, 0.02])
    assert market1 == pytest.approx(0.06)

    values2, market2 = curves[days[0]][2]
    # A：1.10 × 0.90 − 1 = −0.01；B：1.02 × 1.00 − 1 = +0.02
    np.testing.assert_allclose(values2, [-0.01, 0.02])
    assert market2 == pytest.approx((0.99 + 1.02) / 2 - 1)


def test_stats_excess_is_per_sample_against_same_day_market():
    out = _stats(np.array([0.10, -0.02, 0.0, 0.05]), 0.01)
    assert out["samples"] == 4
    assert out["mean"] == pytest.approx(3.25)
    assert out["median"] == pytest.approx(2.5)
    assert out["up_pct"] == pytest.approx(50.0)
    assert out["excess"] == pytest.approx(2.25)
    assert out["beat_pct"] == pytest.approx(50.0)


def test_pooled_stats_flattens_samples_not_cycles():
    items = [
        (np.array([0.10, 0.02]), 0.06),
        (np.array([0.20]), 0.05),
    ]
    out = _pooled_stats(items)
    assert out["samples"] == 3
    assert out["mean"] == pytest.approx(10.67)  # round(32/3, 2)
    assert out["excess"] == pytest.approx(5.0)
    assert out["beat_pct"] == pytest.approx(66.7)


def _row(hits: int, excess: float | None, beat: float | None = 50.0) -> dict:
    return {
        "hits": hits,
        "returns": [
            {"days": 1, "excess": excess, "beat_pct": beat},
            {"days": 10, "excess": excess, "beat_pct": beat},
        ],
    }


def test_verdict_sample_shortage_comes_first():
    assert _verdict(_row(29, 9.9, 99)) == "样本不足"


def test_verdict_effective_needs_excess_and_beat():
    assert _verdict(_row(100, 1.2, 60)) == "有效"
    assert _verdict(_row(100, 1.2, 50)) == "观察"  # 跑赢率必须 >50%
    assert _verdict(_row(100, 1.0, 60)) == "观察"  # 超额必须 >1.0


def test_verdict_noise_needs_enough_samples():
    assert _verdict(_row(250, 0.2, 50)) == "噪声"
    assert _verdict(_row(199, 0.2, 50)) == "观察"  # 样本不足 200，不能判「噪声」
