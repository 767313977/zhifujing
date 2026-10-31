"""回测的入场日与持有期时间轴：停牌、复牌、退出顺延、基准对齐。

这些测试**全部用明确的日期与 OHLC 数字**（任务书 P0-4 的要求）：只断言返回值类型
没有意义 —— 这里要钉住的是「买入日算第 1 个持有交易日」「计划退出日停牌顺延」这两条
口径，以及「基准按实际入场日聚合」。
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import backtest_patterns as bt  # noqa: E402

from app.services.patterns import Bars  # noqa: E402

DAY0 = date(2026, 1, 5)  # 周一，之后按自然日连续编号，够用


def _day(index: int) -> date:
    return DAY0 + timedelta(days=index)


def _series(
    indexes: list[int],
    opens: list[float],
    closes: list[float],
    *,
    highs: list[float] | None = None,
    pcts: list[float] | None = None,
    code: str = "",
) -> Bars:
    open_ = np.asarray(opens, dtype=float)
    close = np.asarray(closes, dtype=float)
    high = np.asarray(highs, dtype=float) if highs is not None else np.maximum(open_, close)
    return Bars(
        dates=[_day(index) for index in indexes],
        open=open_,
        high=high,
        low=np.minimum(open_, close),
        close=close,
        volume=np.full(len(indexes), 1e6),
        amount=np.full(len(indexes), 1e7),
        pct_chg=(
            np.asarray(pcts, dtype=float) if pcts is not None else np.zeros(len(indexes))
        ),
        code=code,
    )


def _axis(count: int = 20) -> list[date]:
    """完整的交易日轴（用「另一只从不停牌的票」的口径，不依赖被测序列）。"""
    return [_day(index) for index in range(count)]


def _by_horizon(outcomes):
    return {item.horizon: item for item in outcomes}


def _outcomes(bars: Bars, entry, axis: list[date], *, hold_from_entry: bool):
    positions = {day: index for index, day in enumerate(axis)}
    return bt._forward_outcomes(
        bars,
        {day: index for index, day in enumerate(bars.dates)},
        axis,
        entry,
        g_entry=positions[entry.date],
        hold_from_entry=hold_from_entry,
    )


# ---------------------------------------------------------------- 入场日


def test_entry_next_open_normal_is_t_plus_1():
    bars = _series(list(range(8)), [10 + i for i in range(8)], [10.5 + i for i in range(8)])
    entry = bt._entry(bars, 2, bt.ENTRY_NEXT_OPEN)
    assert entry.price == 13.0
    assert entry.date == _day(3)
    assert entry.index == 3


def test_entry_next_open_waits_for_reopen_after_one_day_suspension():
    """T+1 停牌：成交日是复牌首日（T+2），不是 T+1。"""
    bars = _series([0, 1, 2, 4, 5, 6], [10, 10, 10, 11, 11, 11], [10, 10, 10, 11, 11, 11])
    entry = bt._entry(bars, 2, bt.ENTRY_NEXT_OPEN)
    assert entry.date == _day(4)
    assert entry.price == 11.0


def test_entry_next_open_waits_two_days_of_suspension():
    """T+1、T+2 都停牌：成交日是 T+3。"""
    bars = _series([0, 1, 2, 5, 6, 7], [10, 10, 10, 12, 12, 12], [10, 10, 10, 12, 12, 12])
    entry = bt._entry(bars, 2, bt.ENTRY_NEXT_OPEN)
    assert entry.date == _day(5)
    assert entry.price == 12.0


def test_entry_next_open_without_next_bar_is_none():
    bars = _series([0, 1], [10, 11], [10, 11])
    assert bt._entry(bars, 1, bt.ENTRY_NEXT_OPEN) is None


def test_entry_close_is_signal_day():
    bars = _series([0, 1, 2], [10, 11, 12], [10.5, 11.5, 12.5])
    entry = bt._entry(bars, 1, bt.ENTRY_CLOSE)
    assert entry.price == 11.5
    assert entry.date == _day(1)


# ---------------------------------------------------------------- 持有期


def test_holding_period_counts_buy_day_as_day_one():
    """正常 T+1：5 日 = 第 5 个持有交易日收盘 = close[t+5]，与历史口径一致。"""
    bars = _series(list(range(16)), [10 + i for i in range(16)], [10.5 + i for i in range(16)])
    entry = bt._entry(bars, 2, bt.ENTRY_NEXT_OPEN)  # 13.0 买入
    by = _by_horizon(_outcomes(bars, entry, _axis(), hold_from_entry=True))
    assert by[1].value == 13.5 / 13.0 - 1
    assert by[1].actual == _day(3)
    assert by[3].actual == _day(5) and by[3].value == 15.5 / 13.0 - 1
    assert by[5].actual == _day(7) and by[5].value == 17.5 / 13.0 - 1
    assert by[10].actual == _day(12)
    assert [by[h].held for h in (1, 3, 5, 10)] == [1, 3, 5, 10]
    assert all(item.rolled is False for item in by.values())


def test_close_mode_keeps_close_t_plus_h():
    """收盘入场口径没被改动：5 日 = close[t+5]（买入日算第 0 天）。"""
    bars = _series(list(range(12)), [10 + i for i in range(12)], [10.5 + i for i in range(12)])
    entry = bt._entry(bars, 2, bt.ENTRY_CLOSE)  # 12.5 收盘买入
    by = _by_horizon(_outcomes(bars, entry, _axis(), hold_from_entry=False))
    assert by[5].actual == _day(7)
    assert by[5].value == 17.5 / 12.5 - 1
    assert by[5].held == 5


def test_holding_period_starts_at_delayed_entry():
    """T+1 停牌、T+2 复牌买入：5 日 = 从复牌日起的第 5 个持有交易日。"""
    bars = _series(
        [0, 1, 2, 4, 5, 6, 7, 8, 9, 10],
        [10, 10, 10, 11, 11, 11, 11, 11, 11, 11],
        [10, 10, 10, 11, 11, 11, 11, 11, 11, 11],
    )
    entry = bt._entry(bars, 2, bt.ENTRY_NEXT_OPEN)
    assert entry.date == _day(4)
    by = _by_horizon(_outcomes(bars, entry, _axis(), hold_from_entry=True))
    # 第 1 个持有交易日 = 周五(1/9，序号 4)，第 5 个 = 1/13（序号 8）
    assert by[1].actual == _day(4)
    assert by[5].actual == _day(8)
    assert by[5].held == 5
    # 若仍按信号日算，5 日会是序号 7 —— 那是错的
    assert by[5].actual != _day(7)


def test_exit_on_suspended_day_rolls_to_next_tradable_close():
    """计划退出日停牌：顺延到复牌后第一个交易日的收盘，并记录实际持有天数。"""
    bars = _series(
        [0, 1, 2, 3, 4, 6, 7],
        [10, 10, 10, 11, 11, 12, 12],
        [10, 10, 10, 11, 11, 13, 14],
    )
    entry = bt._entry(bars, 2, bt.ENTRY_NEXT_OPEN)  # 1/8 开盘 11 买入
    by = _by_horizon(_outcomes(bars, entry, _axis(12), hold_from_entry=True))
    # 3 日目标 = 1/10（序号 5，停牌）→ 顺延到 1/11（序号 6）
    assert by[3].planned == _day(5)
    assert by[3].actual == _day(6)
    assert by[3].rolled is True
    assert by[3].value == 13.0 / 11.0 - 1
    # 买入日（1/8）算第 1 天，实际退出是 1/11 → 只有 4 个持有交易日
    assert by[3].held == 4


def test_exit_missing_at_data_end_is_none_not_zero():
    """数据末端没有目标日：该档为 None（无法退出），**不能**当 0% 或丢掉不计数。"""
    bars = _series([0, 1, 2, 3, 4], [10] * 5, [10] * 5)
    entry = bt._entry(bars, 2, bt.ENTRY_NEXT_OPEN)
    by = _by_horizon(_outcomes(bars, entry, _axis(20), hold_from_entry=True))
    assert by[1].value == 0.0
    assert by[5].value is None and by[5].actual is None
    assert by[5].planned == _day(7)


# ---------------------------------------------------------------- 买不到 / 涨停


def test_reopen_open_at_limit_up_is_unbuyable():
    """复牌首日开盘就在涨停价（主板 10%：前收 10 → 11）—— 买不到。"""
    bars = _series([0, 1, 2, 4], [10, 10, 10, 11], [10, 10, 10, 11])
    assert bt._unbuyable(bars, 2, bt.ENTRY_NEXT_OPEN, 10.0) is True


def test_reopen_open_below_limit_is_buyable():
    bars = _series([0, 1, 2, 4], [10, 10, 10, 10.9], [10, 10, 10, 10.9])
    assert bt._unbuyable(bars, 2, bt.ENTRY_NEXT_OPEN, 10.0) is False


# ---------------------------------------------------------------- 基准对齐


def _records(bars: Bars, name: str = "正常") -> list[dict]:
    return [
        {
            "date": day,
            "name": name,
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


def test_benchmark_keys_by_actual_entry_date():
    """停牌拖延的票，它的基准要落在**实际入场日**，不能并到信号日。

    A（300001）在 1/8 停牌：1/7 的「次日开盘」实际是 1/9 成交，收益 12/10−1=+20%。
    B（600000）每天都交易、价格不动，收益 0。
    所以 1/9 的 1 日基准 = (20% + 0%) / 2 = 10%；1/8 只有 B → 0%。
    """
    a = _series(
        [0, 1, 2, 4, 5],
        [10, 10, 10, 10, 10],
        [10, 10, 10, 12, 12],
        code="300001",
    )
    b = _series([0, 1, 2, 3, 4, 5], [10] * 6, [10] * 6, code="600000")
    grouped = {"300001": _records(a), "600000": _records(b)}
    bars_by_code = {"300001": a, "600000": b}
    axis = _axis(8)
    global_pos = {day: index for index, day in enumerate(axis)}

    bench_all, bench_wudao, _ = bt._benchmark(
        bars_by_code,
        grouped,
        axis,
        global_pos,
        bt.ENTRY_NEXT_OPEN,
        skip_unbuyable=False,
        st_days={},
    )
    # 基准表按**持有期**（1 日 → 键 1）取
    assert bench_all[1][_day(4)] == pytest.approx(0.1)
    assert bench_all[1][_day(3)] == pytest.approx(0.0)
    # 悟道池只含创业板 + 科创板 → 只有 A，1/9 那天是 +20%
    assert bench_wudao[1][_day(4)] == pytest.approx(0.2)


# ---------------------------------------------------------------- ST / 候选池


def test_st_rows_are_per_trading_day():
    """ST 按交易日判：只有带 ST 的那一天被剔，摘帽后可以重新参与。"""
    records = [
        {"date": _day(0), "name": "正常股份"},
        {"date": _day(1), "name": "ST正常"},
        {"date": _day(2), "name": "ST正常"},
        {"date": _day(3), "name": "正常股份"},
    ]
    st_days, stats = bt._st_rows({"600000": records})
    assert st_days == {"600000": {_day(1), _day(2)}}
    assert stats["rows"] == 2
    assert stats["codes"] == 1


def test_st_rows_flags_unrecoverable_history():
    """全历史名称恒为 ST：无法区分「一直 ST」与「后来才 ST」，必须标出来。"""
    records = [{"date": _day(i), "name": "ST某某"} for i in range(3)]
    _, stats = bt._st_rows({"600000": records})
    assert stats["constant_st"] == 1


def test_candidate_sets_follow_production_window():
    """候选池：创业板/科创板 + 冲高 4.5%~22% + 收盘涨跌 −3%~20.5%。"""
    # 昨收 10 → 今开 10、最高 10.6（+6%）、收盘 10.2（+2%）
    hit = _series([0, 1], [10, 10], [10, 10.2], highs=[10, 10.6], pcts=[0, 2.0], code="300001")
    # 冲高 30% 超过上界
    too_high = _series(
        [0, 1], [10, 10], [10, 10], highs=[10, 13], pcts=[0, 0.0], code="300002"
    )
    # 主板不收
    main_board = _series(
        [0, 1], [10, 10], [10, 10.2], highs=[10, 10.6], pcts=[0, 2.0], code="600000"
    )
    sets = bt._candidate_sets(
        {"300001": hit, "300002": too_high, "600000": main_board}
    )
    assert sets[_day(1)] == {"300001"}
