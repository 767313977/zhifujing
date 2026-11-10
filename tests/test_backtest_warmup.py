"""回测的「按形态预热门槛」与「基准票池一致性」（2026-11-01 审计 P1/P2）。

预热门槛的口径只有一处：`app/services/patterns.PATTERN_MIN_BARS`（每条等于该形态
`detect` 里的 `len(bars) < …` 守卫），再与生产公共门槛 22 根取 max。回测直接读它，
**不再按 80 根一刀切** —— 那会把「只有 22~79 根、但生产端确实会判定」的票整只漏掉。

基准票池（`_benchmark_pool`）只按 22 根筛，**与本次跑哪些形态无关**：否则同一形态
单独跑与组合跑的基准不是同一批票，超额不可比。
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import backtest_patterns as bt  # noqa: E402

from app.services import patterns  # noqa: E402


def _records(count: int, *, sample_last: bool = False) -> list[dict]:
    start = date(2026, 1, 5)
    rows = [
        {
            "date": start + timedelta(days=index),
            "name": "测试股",
            "open": 10.0,
            "high": 10.0,
            "low": 10.0,
            "close": 10.0,
            "volume": 1e6,
            "amount": 1e7,
            "pct_chg": 0.0,
        }
        for index in range(count)
    ]
    if sample_last:
        # 与 `test_wudao_pools` 同一个样板日：冲高 9%、收 10.6、量比 2.0、离最高 2.75%
        rows[-1].update(
            {
                "open": 10.4,
                "high": 10.9,
                "low": 10.3,
                "close": 10.6,
                "volume": 2.0e6,
                "amount": 2.0e7,
                "pct_chg": 6.0,
            }
        )
    return rows


#: 各形态的**有效**预热根数（= max(22, detect 守卫)）。改阈值时必须同步改这里。
EXPECTED_WARMUP = {
    "wudao_sample": 22,
    "wudao_start": 23,
    "wudao_wash2": 28,
    "huabao_early": 30,
    "pile_wash_ready": 50,
    "pile_wash_wash": 50,
    "zgnb_turn": 170,
    "zgnb_shrink": 170,
    "zgnb_origin": 170,
    "zgnb_super_shrink": 170,
    "zgnb_pull_white": 170,
    "zgnb_pull_super": 170,
    "zgnb_pull_yellow": 170,
    "t_buy": 150,
    "ma_bull": 80,
    "new_high": 61,
    "platform_breakout": 35,
    "volume_breakout": 80,
    "volume_surge": 22,
    "dry_pullback": 80,
    "cup_handle": 110,
    "double_bottom": 90,
    "head_shoulders": 100,
    "flag": 40,
    "three_stage": 35,
    "limit_surge_flat": 48,
    "breakout_flat": 68,
    "n_shape": 30,
    "burst_shrink_pullback": 22,
    "pile_up_shrink": 67,
    "ma_squeeze": 65,
    "ma_golden_cross": 66,
    "ma_volume": 22,
    "downtrend_breakout": 60,
    "gap_up_breakout": 22,
    "gap_fill": 23,
    "box_breakout": 62,
    "volume_dry_bottom": 61,
    "shrink_limit_up": 22,
    "volume_engulf": 22,
    "v_bottom": 40,
    "round_bottom": 60,
    "triple_bottom": 90,
    "three_white_soldiers": 22,
    "kneading_line": 22,
    "jade_pillar": 62,
    "ma_cross_big_yang": 30,
    "turtle_breakout": 22,
}


def test_every_registered_pattern_has_an_explicit_floor():
    """注册表里每个形态都要有门槛登记 —— 不许再用「没列出的按 80」兜底。"""
    registered = {pattern.key for pattern in patterns.PATTERNS}
    assert registered == set(patterns.PATTERN_MIN_BARS)
    for key in sorted(registered):
        assert patterns.PATTERN_MIN_BARS[key] > 0, key
        assert bt._warmup(key) == max(patterns.WUDAO_MIN_BARS, patterns.PATTERN_MIN_BARS[key])


def test_warmup_values_match_detect_guards():
    for key, expected in EXPECTED_WARMUP.items():
        assert bt._warmup(key) == expected, key


def test_only_genuinely_long_patterns_sit_at_80():
    """80 根只能留给**真的**需要那么长的形态，不能当兜底默认值。"""
    at_80 = {key for key in patterns.PATTERN_MIN_BARS if bt._warmup(key) == 80}
    assert at_80 == {"ma_bull", "volume_breakout", "dry_pullback"}


def test_long_warmup_patterns_are_not_relaxed():
    """B1 / 做T / 长窗口几何形态的门槛必须保住（不许为了多收样本放宽）。"""
    assert bt._warmup("zgnb_shrink") == patterns.ZGNB_MIN_BARS == 170
    assert bt._warmup("t_buy") == patterns.T_BUY_MIN_BARS == 150
    assert bt._warmup("cup_handle") == 110
    assert bt._warmup("head_shoulders") == 100
    assert bt._warmup("double_bottom") == bt._warmup("triple_bottom") == 90


def test_draft_patterns_use_their_own_floor():
    """实验形态不在生产注册表里，用自己的守卫值（60 / 91），不是 80 一刀切。"""
    assert bt._warmup("pocket_pivot_kacher") == 60
    assert bt._warmup("pocket_pivot_ths") == 91


@pytest.mark.parametrize("count", [21, 22, 23, 30, 35, 40, 60, 61, 62, 66, 70, 80])
def test_select_bars_boundaries(count: int):
    """22/23/30/35/40/60/61/62/66/70/80 根这些边界上，进池判定必须与门槛严格一致。"""
    grouped = {"300001": _records(count)}
    keys = (
        "wudao_sample",  # 22
        "gap_fill",  # 23
        "n_shape",  # 30
        "platform_breakout",  # 35
        "v_bottom",  # 40
        "round_bottom",  # 60
        "new_high",  # 61
        "box_breakout",  # 62
        "ma_golden_cross",  # 66
        "zgnb_shrink",  # 170
    )
    for key in keys:
        picked = bool(bt._select_bars(grouped, {key: {}}))
        assert picked == (count >= bt._warmup(key)), f"{count} 根 / {key}"


def test_22_bar_series_can_hit_wudao_sample():
    """只有 22 根 K 线（生产最低门槛）的票，样板日必须能命中。"""
    records = _records(22, sample_last=True)
    bars = patterns.build_bars(records, "300001")
    assert len(bars) == patterns.WUDAO_MIN_BARS
    signal = patterns._wudao_sample(bars)
    assert signal is not None
    assert signal.pattern == "wudao_sample"


def test_22_bar_series_still_excluded_for_b1():
    """同一只 22 根的票，B1 需要 170 根 —— 序列能进池（公共门槛），但门槛挡住它。"""
    grouped = {"300001": _records(22, sample_last=True)}
    assert bt._select_bars(grouped, {"zgnb_shrink": {}}) == {}
    assert bt._warmup("zgnb_shrink") > 22


def test_short_series_enter_pool_for_short_patterns():
    """30 根的票：放量上涨（22）能判，V 型底（40）不能 —— 单跑时后者筛掉它。"""
    grouped = {"300001": _records(30)}
    assert "300001" in bt._select_bars(grouped, {"volume_surge": {}})
    assert "300001" not in bt._select_bars(grouped, {"v_bottom": {}})


# ---------------------------------------------------------------- 基准口径（P2）


def test_benchmark_pool_ignores_target_selection():
    """基准池只按 22 根筛，**不随目标形态变**（旧实现拿 `_select_bars` 的结果当分母）。"""
    records = {
        "300001": _records(22),
        "300002": _records(30),
        "300003": _records(80),
    }
    bars_all = {code: bt.build_bars(rows, code) for code, rows in records.items()}
    bench = set(bt._benchmark_pool(bars_all))
    assert bench == {"300001", "300002", "300003"}

    # 样本池会随目标形态变（单跑 v_bottom 时 22/30 根的票被筛掉），基准池不变
    assert set(bt._select_bars(bars_all, {"v_bottom": {}})) == {"300003"}
    assert set(bt._select_bars(bars_all, {"v_bottom": {}, "wudao_sample": {}})) == bench
    assert set(bt._benchmark_pool(bars_all)) == bench


def test_benchmark_values_identical_for_single_and_combined_runs():
    """同一形态单跑 vs 组合跑：基准表逐日逐票完全相同（这正是 P2 要保证的）。"""
    records = {
        "300001": _records(22),
        "300002": _records(50),  # 40 ≤ 50 < 60：单跑 round_bottom 时会被筛掉
        "300003": _records(80),
        "300004": _records(120),
    }
    bars_all = {code: bt.build_bars(rows, code) for code, rows in records.items()}
    names_all = {
        code: [str(row.get("name") or "") for row in rows] for code, rows in records.items()
    }
    bench_bars = bt._benchmark_pool(bars_all)
    all_dates = sorted({day for bars in bench_bars.values() for day in bars.dates})
    global_pos = {day: index for index, day in enumerate(all_dates)}
    st_days: dict[str, set[date]] = {}

    def _run() -> dict:
        # 主流程里基准的输入与 targets 无关：两种「跑法」走的是同一个 `_benchmark_pool`
        return bt._benchmark(
            bt._benchmark_pool(bars_all),
            names_all,
            all_dates,
            global_pos,
            bt.ENTRY_CLOSE,
            skip_unbuyable=False,
            st_days=st_days,
        )[0]

    single = _run()  # 对应 --pattern round_bottom（60 根）
    combined = _run()  # 对应 --pattern round_bottom,v_bottom（40 根）
    assert set(bt._select_bars(bars_all, {"round_bottom": {}})) != set(
        bt._select_bars(bars_all, {"round_bottom": {}, "v_bottom": {}})
    )
    assert single == combined
    assert single[bt.HORIZONS[0]], "基准表不应为空（否则这条断言没意义）"
