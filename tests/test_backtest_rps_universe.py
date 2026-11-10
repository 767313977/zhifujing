"""P0 回归：横截面 RPS 的分母必须是**全池**，不随 `--stocks` 抽样变化。

背景：旧实现里 `--stocks N` 先截断代码，再加载 K 线、再算 RPS —— 于是 800 只抽样
会把「全市场百分位」算成「800 只内百分位」。修复后：
- `universe_codes` 决定 RPS 分母（全池）；
- `research_codes` 只决定形态评价集合。

本文件用确定性合成序列验证：同一票同一日，RPS50/120/250 在任意抽样比例下完全一致。
"""

from __future__ import annotations

import math
import sys
from array import array
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts import backtest_patterns as bp  # noqa: E402

N_CODES = 20
N_DAYS = 300


def _synthetic_series(n_codes: int = N_CODES, n_days: int = N_DAYS):
    """确定性合成序列：不同票给不同斜率与正弦扰动，保证排名有区分度。"""
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(n_days)]
    out: dict[str, bp._RpsSeries] = {}
    for i in range(n_codes):
        values = array("f")
        for j in range(n_days):
            pct = ((i % 7) - 3) * 0.12 + math.sin(j * 0.3 + i) * 1.1
            values.append(round(pct, 2))
        out[f"{i:06d}"] = bp._RpsSeries(dates=list(days), pct_chg=values)
    return out, days


@pytest.fixture(scope="module")
def synthetic():
    return _synthetic_series()


# ---- 核心：抽样规模不改变 RPS 值 --------------------------------------------

@pytest.mark.parametrize("sample_ratio", [1.0, 0.8, 0.2])
def test_rps_identical_regardless_of_sample_size(synthetic, sample_ratio):
    """全池算 RPS 后，取 100% / 80% / 20% 的票查值，重叠部分必须完全相同。"""
    series, days = synthetic
    all_dates = sorted(days)

    universe_ctx = bp._rps_context(series, {50, 120, 250}, all_dates)

    cut = max(1, int(len(series) * sample_ratio))
    research_codes = list(series)[:cut]
    research_bars = {code: series[code] for code in research_codes}

    ctx = bp._filter_context(
        ["rps50_90", "rps120_85", "rps_any_90"],
        research_bars,
        all_dates,
        rps_bars=series,
    )

    checked = 0
    for code in research_codes:
        for idx in range(260, len(days), 7):
            # 用 index 而非日期，避免日期对象同一性问题干扰
            for window in (50, 120, 250):
                got = bp._rps_lookup(ctx, window, code, days[idx])
                want = bp._rps_lookup(universe_ctx, window, code, days[idx])
                assert got == want, (
                    f"抽样比例 {sample_ratio} 下 RPS{window} 不一致："
                    f"{code} @ {days[idx]} got={got} want={want}"
                )
                checked += 1
    assert checked > 0


def test_rps_differs_if_subset_used_as_denominator(synthetic):
    """反例：用子集当分母时值会变化 —— 证明上一条测试具备区分力。"""
    series, days = synthetic
    all_dates = sorted(days)

    research_codes = list(series)[:4]  # 20%
    research_bars = {code: series[code] for code in research_codes}

    correct = bp._filter_context(
        ["rps50_90"], research_bars, all_dates, rps_bars=series
    )
    wrong = bp._filter_context(["rps50_90"], research_bars, all_dates)  # 子集当分母

    differs = 0
    for code in research_codes:
        for idx in range(60, len(days), 3):
            a = bp._rps_lookup(correct, 50, code, days[idx])
            b = bp._rps_lookup(wrong, 50, code, days[idx])
            if a != b:
                differs += 1
    assert differs > 0, "子集分母与全池分母结果相同，测试无区分力"


# ---- RPS 筛选与独立计算一致 -------------------------------------------------

def test_rps90_filter_matches_independent_ranking(synthetic):
    """RPS50 ≥ 90 的判定必须与「独立按全池排名算出的百分位」一致。"""
    series, days = synthetic
    all_dates = sorted(days)
    ctx = bp._rps_context(series, {50}, all_dates)

    day = days[-1]
    # 独立计算：当天所有票的 50 日复利涨幅 → 中位秩
    from app.services import rps as rps_rules

    values = {}
    for code, s in series.items():
        pos = {d: i for i, d in enumerate(s.dates)}
        g = pos[day]
        start = g - 50 + 1
        logs = [0.0]
        for pct in s.pct_chg.tolist():
            logs.append(logs[-1] + math.log1p(pct / 100.0))
        lo = max(0, start)
        if g - lo + 1 < int(50 * rps_rules.MIN_COVERAGE):
            continue
        values[code] = (math.exp(logs[g + 1] - logs[lo]) - 1) * 100
    ordered = sorted(values.values())

    for code, ret in values.items():
        expected = rps_rules.mid_rank(ordered, ret)
        actual = bp._rps_lookup(ctx, 50, code, day)
        assert actual is not None
        assert actual == pytest.approx(expected, abs=0.11), (
            f"{code} RPS50 与独立计算不一致：{actual} vs {expected}"
        )


# ---- 未预热保持 NULL --------------------------------------------------------

def test_no_rps_before_warmup(synthetic):
    """250 日窗口未完成预热时必须为 None，不能拿半截窗口冒充。"""
    series, days = synthetic
    all_dates = sorted(days)
    ctx = bp._rps_context(series, {250}, all_dates)

    code = list(series)[0]
    assert bp._rps_lookup(ctx, 250, code, days[100]) is None
    assert bp._rps_lookup(ctx, 250, code, days[248]) is None
    assert bp._rps_lookup(ctx, 250, code, days[260]) is not None
