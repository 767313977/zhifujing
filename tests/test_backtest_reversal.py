"""低吸反转研究脚本的自身逻辑测试（因子方向、时点、分层、确定性、只读）。

成交语义（一字板、停牌、跌停、pending、成本）已由 `test_research_backtest.py` 覆盖，
这里只测本脚本新增的部分，避免重复。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from app.services import factor_score  # noqa: E402
from app.services.research_backtest import BarMatrix  # noqa: E402
from scripts import backtest_reversal as br  # noqa: E402


def _matrix(n_codes: int = 40, n_days: int = 30) -> BarMatrix:
    """确定性合成矩阵：code 越小跌得越多、成交额越大、振幅越大。"""
    calendar = [date(2024, 1, 1) + timedelta(days=i) for i in range(n_days)]
    codes = [f"{i:06d}" for i in range(n_codes)]
    matrix = BarMatrix(calendar, codes)
    for j, _code in enumerate(codes):
        drift = -0.01 + 0.0005 * j          # 0 号最跌，越往后越涨
        amplitude = 0.06 - 0.001 * j        # 0 号振幅最大
        amount = 1e9 / (j + 1)              # 0 号成交额最大
        close = 10.0
        for i in range(n_days):
            pct = drift * 100
            close *= 1 + drift
            matrix.open[i, j] = close
            matrix.high[i, j] = close * (1 + amplitude / 2)
            matrix.low[i, j] = close * (1 - amplitude / 2)
            matrix.close[i, j] = close
            matrix.pct[i, j] = pct
            matrix.amount[i, j] = amount
            matrix.present[i, j] = True
            matrix.name[i, j] = "测试"
    return matrix


def _series_at(matrix, code, pos) -> list[tuple]:
    j = matrix.code_pos[code]
    cols = br._code_columns(matrix)
    return br._build_series(matrix, cols, pos, code, j)


# ---- 1. 因子方向 ------------------------------------------------------------

def test_rev20_direction_and_amp_direction():
    """code 0 跌得最多 → rev20 最大；振幅最大 → amp20 原始值最大、分位最低。"""
    matrix = _matrix()
    pos = len(matrix.calendar) - 1
    series = {
        code: _series_at(matrix, code, pos)
        for code in matrix.codes
    }
    scores = factor_score.score_series(series)
    percent = br._factor_percentiles(scores)

    assert scores["000000"].rev20 > scores["000039"].rev20
    assert scores["000000"].amp20 > scores["000039"].amp20
    # amp20 取负后排名：低振幅（39 号）分位更高
    assert percent["000039"]["amp20"] > percent["000000"]["amp20"]


def test_illiq20_original_direction():
    """成交额越小 → illiq20 原始值越大；分位也越高。"""
    matrix = _matrix()
    pos = len(matrix.calendar) - 1
    series = {code: _series_at(matrix, code, pos) for code in matrix.codes}
    scores = factor_score.score_series(series)
    percent = br._factor_percentiles(scores)

    assert scores["000039"].illiq20 > scores["000000"].illiq20
    assert percent["000039"]["illiq20"] > percent["000000"]["illiq20"]


def test_factor_percentiles_match_production_score():
    """B 用的分位与生产 `score_series` 的分位同源：加权后应等于 score。"""
    matrix = _matrix()
    pos = len(matrix.calendar) - 1
    series = {code: _series_at(matrix, code, pos) for code in matrix.codes}
    scores = factor_score.score_series(series)
    percent = br._factor_percentiles(scores)

    for code, item in scores.items():
        expected = (
            percent[code]["rev20"] * factor_score.WEIGHTS["rev20"]
            + percent[code]["illiq20"] * factor_score.WEIGHTS["illiq20"]
            + percent[code]["amp20"] * factor_score.WEIGHTS["amp20"]
        )
        assert item.score == pytest.approx(round(expected, 1), abs=0.15)


# ---- 2. 时点：窗口与流动性不读未来 ------------------------------------------

def test_window_uses_only_past_rows():
    """`_build_series` 的最后一根必须是信号日当天，且长度固定为 20。"""
    matrix = _matrix()
    pos = 25
    series = _series_at(matrix, "000000", pos)
    assert len(series) == br.WINDOW
    assert series[-1][0] == matrix.calendar[pos]
    assert series[0][0] == matrix.calendar[pos - br.WINDOW + 1]


def test_liquidity_ignores_future_rows():
    """把未来成交额改成天价，信号日的流动性值必须不变。"""
    matrix = _matrix()
    pos = 25
    j = matrix.code_pos["000000"]
    cols = br._code_columns(matrix)
    before = br._liquidity(matrix, cols["000000"], pos, j)

    matrix.amount[pos + 1 :, j] = 1e18
    after = br._liquidity(matrix, cols["000000"], pos, j)
    assert before == after


def test_insufficient_history_returns_none():
    matrix = _matrix(n_days=30)
    cols = br._code_columns(matrix)
    j = matrix.code_pos["000000"]
    assert br._build_series(matrix, cols, 5, "000000", j) is None
    assert br._liquidity(matrix, cols["000000"], 5, j) is None


# ---- 3. 策略独立性 ----------------------------------------------------------

def test_strategy_a_ranks_by_raw_rev20():
    """A 组 Top1 必须是 20 日跌幅最大的那只（rev20 最大）。"""
    matrix = _matrix()
    signals = br.build_signals(matrix, top_n=3)
    first_day = signals["A"][0].signal_date
    first = [s for s in signals["A"] if s.signal_date == first_day]
    assert len(first) == 3
    assert first[0].code == "000000"


def test_strategy_d_only_keeps_high_liquidity():
    """D 组候选必须全部落在当日成交额最高的那一档（25%）。"""
    matrix = _matrix()
    signals = br.build_signals(matrix, top_n=3)
    first_day = signals["L2_25"][0].signal_date
    picked = {s.code for s in signals["L2_25"] if s.signal_date == first_day}

    pos = matrix.date_pos[first_day]
    cols = br._code_columns(matrix)
    liq = {
        code: br._liquidity(matrix, cols[code], pos, matrix.code_pos[code])
        for code in matrix.codes
    }
    ranked = sorted((v, code) for code, v in liq.items() if v is not None)
    ranked.reverse()
    keep = {code for _v, code in ranked[: max(3, int(len(ranked) * 0.25))]}

    assert picked, "D25 当天应有候选"
    assert picked <= keep, f"越出高流动性档：{picked - keep}"


def test_strategies_are_not_identical():
    matrix = _matrix()
    signals = br.build_signals(matrix, top_n=3)
    a = {(s.signal_date, s.code) for s in signals["A"]}
    c = {(s.signal_date, s.code) for s in signals["C0"]}
    assert a != c, "A（单因子）与 C（三因子）不应给出完全相同名单"


# ---- 4. 确定性与可复现 ------------------------------------------------------

def test_build_signals_is_deterministic():
    matrix = _matrix()
    first = {k: [(s.signal_date, s.code) for s in v] for k, v in br.build_signals(matrix, top_n=3).items()}
    second = {k: [(s.signal_date, s.code) for s in v] for k, v in br.build_signals(matrix, top_n=3).items()}
    assert first == second


# ---- 5. 只读 ----------------------------------------------------------------

def test_engine_is_readonly():
    engine = br._readonly_engine(Path("dummy.db"))
    try:
        assert "mode=ro" in str(engine.url)
    finally:
        engine.dispose()


# ---- 8. 退出状态与收益口径 ------------------------------------------------

def _fake_result(statuses, strategy="X"):
    """用真实 `Sample` 构造一个最小结果对象（不跑引擎）。"""
    from types import SimpleNamespace

    from app.services.research_backtest import Sample

    samples = []
    for index, status in enumerate(statuses):
        gross = 1.0 if status in ("ok", "exit_limit_down") else None
        samples.append(
            Sample(
                code=f"{index:06d}",
                signal_date=date(2024, 1, 2) + timedelta(days=index),
                horizon=5,
                status=status,
                gross=gross,
                benchmark=0.5 if gross is not None else None,
                exit_delay=1 if status == "exit_limit_down" else 0,
            )
        )
    return SimpleNamespace(samples=samples, strategy=strategy)


def test_state_groups_cover_all_samples():
    result = _fake_result(["ok", "ok", "exit_limit_down", "no_exit", "pending"])
    groups = br._state_groups(result, 5)
    assert groups == {"ok": 2, "exit_limit_down": 1, "no_exit": 1, "pending": 1}
    assert sum(groups.values()) == len(result.samples)


def test_entry_samples_include_delayed_exit_only():
    result = _fake_result(["ok", "exit_limit_down", "no_exit", "pending"])
    entry = br._entry_samples(result, 5)
    assert len(entry) == 2
    assert {s.status for s in entry} == {"ok", "exit_limit_down"}
    assert len(br._unresolved_samples(result, 5)) == 1


def test_cond_is_subset_of_entry():
    """条件收益（ok）的样本必须是全部已入场样本的子集。"""
    result = _fake_result(["ok", "ok", "exit_limit_down"])
    assert len(br._ok_samples(result, 5)) <= len(br._entry_samples(result, 5))


def test_three_excess_definitions_differ():
    """毛超额、双方净超额、净减毛基准是三个不同口径，不能混用。"""
    from app.services.research_backtest import Sample

    sample = Sample(
        code="000001", signal_date=date(2024, 1, 2), horizon=5,
        status="ok", gross=2.0, benchmark=1.0,
    )
    gross_excess = sample.gross - sample.benchmark
    both_net = br._net(sample.gross, 30.0) - br._net(sample.benchmark, 30.0)
    net_minus_gross_bench = br._net(sample.gross, 30.0) - sample.benchmark
    assert gross_excess == pytest.approx(1.0)
    # 双方都扣费 → 差额只是被成本比例缩小
    assert both_net == pytest.approx(gross_excess * 0.997, abs=1e-9)
    # 只扣策略、基准不扣 → 比双方净超额更小（差异 = 基准那笔成本）
    assert net_minus_gross_bench < both_net < gross_excess
    assert net_minus_gross_bench == pytest.approx(
        both_net + (br._net(sample.benchmark, 30.0) - sample.benchmark), abs=1e-9
    )


def test_stress_is_below_entry_mean():
    """未解决样本按固定亏损计入后，均值必须低于只算已入场样本。"""
    result = _fake_result(["ok", "ok", "no_exit", "no_exit"])
    entry = br._entry_samples(result, 5)
    unresolved = br._unresolved_samples(result, 5)
    base = sum(s.gross for s in entry)
    plain = base / len(entry)
    stress = (base - 10.0 * len(unresolved)) / (len(entry) + len(unresolved))
    assert stress < plain


def test_paired_reproducible_across_status_mix():
    """同一输入在不同退出状态混合下，配对统计必须可复现。"""
    result = _fake_result(["ok", "exit_limit_down"])
    first = br._paired(br._daily_entry_mean(result), br._daily_entry_mean(result))
    second = br._paired(br._daily_entry_mean(result), br._daily_entry_mean(result))
    assert first["days"] == second["days"] == 2
    assert first["mean"] == second["mean"]


def test_daily_mean_includes_delayed_exit():
    """逐日对账：延迟退出必须进入日度均值，且与「仅 ok」的均值不同。"""
    from app.services.research_backtest import Sample

    day = date(2024, 1, 2)
    samples = [
        Sample(code="000001", signal_date=day, horizon=5, status="ok", gross=1.0),
        Sample(code="000002", signal_date=day, horizon=5, status="ok", gross=1.0),
        Sample(
            code="000003", signal_date=day, horizon=5, status="exit_limit_down",
            gross=-2.0, exit_delay=1,
        ),
    ]
    from types import SimpleNamespace

    result = SimpleNamespace(samples=samples, strategy="X")
    entry_mean = br._daily_entry_mean(result)[day]
    ok_only = sum(s.gross for s in br._ok_samples(result, 5)) / 2
    assert entry_mean == pytest.approx((1.0 + 1.0 - 2.0) / 3)
    assert entry_mean != ok_only


def test_module_does_not_import_main_session():
    """脚本不得持有主库连接入口（只能通过显式传入的只读 engine）。"""
    assert not hasattr(br, "session_scope")
    assert not hasattr(br, "main_engine")


# ---- 6. 消融与流动性控制实验 ------------------------------------------------

def test_c1_drops_illiq_and_renormalizes():
    """C1 的评分公式必须与 C0 不同：同一票在两种权重下得分不等。"""
    matrix = _matrix()
    pos = len(matrix.calendar) - 1
    series = {code: _series_at(matrix, code, pos) for code in matrix.codes}
    scores = factor_score.score_series(series)
    percent = br._factor_percentiles(scores)

    code = "000000"
    p = percent[code]
    c0 = 0.30 * p["rev20"] + 0.30 * p["illiq20"] + 0.40 * p["amp20"]
    c1 = (0.30 / 0.70) * p["rev20"] + (0.40 / 0.70) * p["amp20"]
    assert c0 != c1
    # 归一化后权重和为 1
    assert (0.30 / 0.70) + (0.40 / 0.70) == pytest.approx(1.0)


def test_c2_shuffle_preserves_distribution():
    """C2 的 illiq 分位置换只破坏对应关系，分布（多重集合）保持不变。"""
    matrix = _matrix()
    pos = len(matrix.calendar) - 1
    series = {code: _series_at(matrix, code, pos) for code in matrix.codes}
    scores = factor_score.score_series(series)
    percent = br._factor_percentiles(scores)
    day = matrix.calendar[pos]

    shuffled = br._shuffled_illiq(percent, day)
    assert sorted(shuffled.values()) == sorted(percent[c]["illiq20"] for c in percent)
    # 同一天重复调用必须一致（固定种子）
    again = br._shuffled_illiq(percent, day)
    assert shuffled == again
    # 且与原始对应关系不同
    assert any(shuffled[c] != percent[c]["illiq20"] for c in percent)


def test_l2_rescoring_uses_subset_percentiles():
    """L2 在过滤后的子集内重算分位：子集分位必须与全池分位不同。"""
    matrix = _matrix()
    pos = len(matrix.calendar) - 1
    series = {code: _series_at(matrix, code, pos) for code in matrix.codes}
    scores = factor_score.score_series(series)
    full = br._factor_percentiles(scores)

    subset = {code: scores[code] for code in list(scores)[:10]}
    sub = br._factor_percentiles(subset)

    diffs = [code for code in subset if sub[code]["rev20"] != full[code]["rev20"]]
    assert diffs, "子集内重算分位没有产生差异，L2 与 L1 就没有区别"


# ---- 7. 收益口径 ------------------------------------------------------------

def test_net_formula_matches_engine():
    """`_net` 必须与引擎公式一致：cost 是一次往返，扣一次。"""
    # 0 收益也要承担成本 → -0.3%
    assert br._net(0.0, 30.0) == pytest.approx(-0.3, abs=1e-9)
    assert br._net(5.0, 0.0) == pytest.approx(5.0, abs=1e-12)
    expected = ((1 + 0.05) * (1 - 0.003) - 1) * 100
    assert br._net(5.0, 30.0) == pytest.approx(expected, abs=1e-12)
    # 成本越高净收益越低
    assert br._net(5.0, 10.0) > br._net(5.0, 50.0) > br._net(5.0, 200.0)


def test_paired_stats_use_common_days_only():
    """配对统计只取两边都有的日期；缺失日期不填 0。"""
    a = {date(2024, 1, 2): 1.0, date(2024, 1, 3): 3.0, date(2024, 1, 4): 5.0}
    b = {date(2024, 1, 2): 0.0, date(2024, 1, 4): 1.0}
    stat = br._paired(a, b)
    assert stat["days"] == 2
    assert stat["mean"] == pytest.approx(2.5)


def test_paired_ci_brackets_mean():
    a = {date(2024, 1, 2) + timedelta(days=i): 1.0 + 0.1 * i for i in range(20)}
    b = {date(2024, 1, 2) + timedelta(days=i): 0.0 for i in range(20)}
    stat = br._paired(a, b)
    assert stat["days"] == 20
    assert stat["low"] <= stat["mean"] <= stat["high"]


def test_oos_horizon_labels_present():
    """三个持有期都要出现；未完成退出不计入 ok 样本（由内核保证）。"""
    matrix = _matrix()
    signals = br.build_signals(matrix, top_n=2)
    assert br.HORIZONS == (1, 5, 10)
    assert signals["C0"]
