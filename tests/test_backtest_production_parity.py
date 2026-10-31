"""生产口径对齐：板块闸门、ST 按交易日、候选池范围。

对应任务书 P0-5。这里只测**纯函数**，不跑全量回测。
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import backtest_patterns as bt  # noqa: E402

from app.jobs.scan_patterns import (  # noqa: E402
    WUDAO_POOL_KEYS,
    _signal_allowed,
    is_pattern_board,
    is_wudao_board,
    pattern_board_allowed,
)
from app.services.patterns import Bars  # noqa: E402

DAY0 = date(2026, 1, 5)


def _day(index: int) -> date:
    return DAY0 + timedelta(days=index)


def _flat(indexes: list[int], code: str = "600000") -> Bars:
    values = np.full(len(indexes), 10.0)
    return Bars(
        dates=[_day(index) for index in indexes],
        open=values.copy(),
        high=values.copy(),
        low=values.copy(),
        close=values.copy(),
        volume=np.full(len(indexes), 1e6),
        amount=np.full(len(indexes), 1e7),
        pct_chg=np.zeros(len(indexes)),
        code=code,
    )


# ---------------------------------------------------------------- 板块


def test_prod_board_excludes_beijing():
    assert is_pattern_board("600000") and is_pattern_board("000001")
    assert is_pattern_board("300001") and is_pattern_board("688001")
    assert not is_pattern_board("830001")  # 北交所 30cm
    assert not is_pattern_board("430001")


def test_wudao_board_is_chinext_and_star_only():
    assert is_wudao_board("300001") and is_wudao_board("301001")
    assert is_wudao_board("302132") and is_wudao_board("688001")
    assert not is_wudao_board("600000")
    assert not is_wudao_board("000001")
    assert not is_wudao_board("830001")


def test_wudao_pools_do_not_leak_into_main_board():
    """悟道六池 + 金叉大阳在 10cm 主板上必须被挡（阈值按 20cm 校准）。"""
    for key in WUDAO_POOL_KEYS:
        assert pattern_board_allowed(key, "300001") is True
        assert pattern_board_allowed(key, "600000") is False
    # 普通形态放开主板
    assert pattern_board_allowed("v_bottom", "600000") is True
    # 北交所任何形态都不收
    assert pattern_board_allowed("v_bottom", "830001") is False


def test_signal_allowed_uses_the_same_board_gate():
    """`_signal_allowed` 与回测共用 `pattern_board_allowed`，不能各写一份。"""
    assert _signal_allowed("wudao_sample", "300001", {"300001"}, set()) is True
    assert _signal_allowed("wudao_sample", "600000", {"600000"}, set()) is False
    # 候选池只对明天盯 / 明天预案生效
    assert _signal_allowed("wudao_sample", "300001", set(), set()) is False
    assert _signal_allowed("pile_wash_ready", "300001", set(), set()) is True


# ---------------------------------------------------------------- ST / 基准


def _records(bars: Bars, names: list[str]) -> list[dict]:
    return [
        {
            "date": day,
            "name": names[i],
            "open": float(bars.open[i]),
            "high": float(bars.high[i]),
            "low": float(bars.low[i]),
            "close": float(bars.close[i]),
            "volume": float(bars.volume[i]),
            "amount": float(bars.amount[i]),
            "pct_chg": float(bars.pct_chg[i]),
        }
        for i, day in enumerate(bars.dates)
    ]


def test_benchmark_drops_st_days_for_the_whole_series():
    """某只票某天是 ST：那天的样本与基准都不算 —— 不是整段剔除。"""
    bars = _flat(list(range(6)))
    names = ["正常股份", "正常股份", "ST某某", "正常股份", "正常股份", "正常股份"]
    grouped = {"600000": _records(bars, names)}
    axis = [_day(index) for index in range(6)]
    st_days, stats = bt._st_rows(grouped)
    assert stats["rows"] == 1 and stats["rows"] == len(st_days["600000"])

    bench_all, _, bench_stats = bt._benchmark(
        {"600000": bars},
        grouped,
        axis,
        {day: index for index, day in enumerate(axis)},
        bt.ENTRY_NEXT_OPEN,
        skip_unbuyable=False,
        st_days=st_days,
    )
    # 「2 号入场」没有：那是 ST 日；「3 号入场」也没有：它的信号日是 2 号（ST）。
    assert _day(2) not in bench_all[1]
    assert _day(3) not in bench_all[1]
    # 其余入场日照常在（1 号、4 号、5 号）
    assert _day(1) in bench_all[1] and _day(4) in bench_all[1]
    # 2 号那一天：既作为信号日被剔，也作为 1 号的成交日被剔 → 计 2 次
    assert bench_stats["st"] == 2


def test_st_days_do_not_remove_non_st_portion():
    """摘帽之后那段必须能重新参与 —— 旧实现会把整只票剔掉。"""
    bars = _flat(list(range(5)))
    names = ["ST某某", "ST某某", "正常股份", "正常股份", "正常股份"]
    _, stats = bt._st_rows({"600000": _records(bars, names)})
    assert stats["rows"] == 2
    # 全历史名称有两个不同值 → 不属于「无法还原」的那一类
    assert stats["constant_st"] == 0
