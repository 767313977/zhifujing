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
    first_day = signals["D25"][0].signal_date
    picked = {s.code for s in signals["D25"] if s.signal_date == first_day}

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
    c = {(s.signal_date, s.code) for s in signals["C"]}
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


def test_module_does_not_import_main_session():
    """脚本不得持有主库连接入口（只能通过显式传入的只读 engine）。"""
    assert not hasattr(br, "session_scope")
    assert not hasattr(br, "main_engine")
