"""amp20 方向回归：振幅越低 → amp20 分位越高（生产 `score_series` 已取负号）。

背景：B3 报告里 amp20 的 IC 为 −0.0794，容易被误读成「生产把 amp20 当正向因子」。
实际 `score_series()` 对 amp20 取了负号（`signal = -raw[...] if key == "amp20"`），
所以 IC 为负恰好对应「振幅越低越好」。本文件把这条语义钉死。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services import factor_score  # noqa: E402


def _series(amplitude: float, days: int = 20, amount: float = 1e8) -> list[tuple]:
    """构造振幅固定、其他因子相同的 20 根序列。

    `pct` 全部相同 → rev20 相同；`amount` 相同 → illiq20 相同。
    """
    base = date(2024, 1, 1)
    out = []
    for i in range(days):
        close = 10.0
        high = close * (1 + amplitude / 2)
        low = close * (1 - amplitude / 2)
        out.append((base + timedelta(days=i), 0.5, high, low, close, amount))
    return out


def test_lower_amplitude_gets_higher_amp_score():
    """三只票只有振幅不同：振幅 1% 的总分应高于 3%，3% 高于 5%。"""
    result = factor_score.score_series({
        "A": _series(0.01),
        "B": _series(0.03),
        "C": _series(0.05),
    })

    assert result["A"].amp20 < result["B"].amp20 < result["C"].amp20
    # 其他两个因子完全相同，总分只由 amp20 的池内分位决定
    assert result["A"].score > result["B"].score > result["C"].score


def test_amp20_direction_is_negative_signal():
    """反向确认：若把 amp20 当正向因子，排序会完全颠倒。"""
    result = factor_score.score_series({
        "quiet": _series(0.01),
        "noisy": _series(0.06),
    })
    assert result["quiet"].score > result["noisy"].score
    # 权重表里 amp20 是 0.40，赋给的是「方向调整后的分位」
    assert factor_score.WEIGHTS["amp20"] == 0.40


def test_identical_series_scores_identically():
    """完全相同的两只票分数必须相同（并列同分，不因输入顺序变化）。"""
    result = factor_score.score_series({"X": _series(0.02), "Y": _series(0.02)})
    assert result["X"].score == result["Y"].score
