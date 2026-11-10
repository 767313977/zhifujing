"""B3 内存重构造的等价性对账：流式列式加载 vs 旧字典加载。

数据集故意包含：ST 名称中途变化、字段缺失的残行、停牌造成的日期空洞。
逐项比较（不比较最终均值）：

- 代码集合、日期序列、7 个价格/量数组（精确相等）
- 每票逐日名称
- ST 判定（st_days 与统计）
- `_select_bars` / `_benchmark_pool` 的入选集合
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts import backtest_patterns as bp  # noqa: E402


def _make_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE stock_daily (
                trade_date TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                open REAL, high REAL, low REAL, close REAL,
                pct_chg REAL, volume REAL, amount REAL,
                PRIMARY KEY (trade_date, code)
            )
            """
        )
        rows = []
        base = date(2024, 1, 1)
        for c in range(30):
            code = f"{c:06d}"
            close = 10.0 + c
            for d in range(200):
                # 停牌：每 17 天缺一根，制造日期空洞
                if d % 17 == 0 and d > 0:
                    continue
                day = (base.toordinal() + d)
                day_str = date.fromordinal(day).isoformat()
                close *= 1.0 + ((c + d) % 7 - 3) * 0.004
                # 前 5 只票在第 100 天改名成 ST
                name = "ST测试" if (c < 5 and d >= 100) else f"票{c:02d}"
                open_, high, low = close * 0.995, close * 1.01, close * 0.99
                pct = ((c + d) % 7 - 3) * 0.4
                volume = 1e6 + (c * 1000) + d
                amount = 1e7 + (c * 100) + d
                # 残行：第 3 只票每 23 天把 low 置空，应被 usable_bar 剔除
                if c == 3 and d % 23 == 0:
                    low = None
                rows.append((day_str, code, name, open_, high, low, close, pct, volume, amount))
        conn.executemany(
            "INSERT OR REPLACE INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?)", rows
        )


@pytest.fixture(scope="module")
def db_path(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("parity") / "history.db"
    _make_db(path)
    return path


@pytest.fixture(scope="module")
def both(db_path):
    engine = bp._readonly_engine(db_path)
    codes = [f"{c:06d}" for c in range(30)]
    try:
        old_latest, old_grouped = bp._load_history(engine, codes, 200)
        new = bp._load_history_compact(engine, codes, 200)
    finally:
        engine.dispose()
    return old_latest, old_grouped, new


def test_same_code_set_and_latest(both):
    old_latest, old_grouped, new = both
    assert new.latest == old_latest
    assert set(new.bars) == set(old_grouped)


def test_bars_arrays_identical(both):
    """逐票逐列比较（不比较均值）。"""
    _latest, old_grouped, new = both
    for code, records in old_grouped.items():
        expected = bp.build_bars(records, code)
        actual = new.bars[code]
        assert actual.dates == expected.dates, f"{code} 日期序列不一致"
        for field in ("open", "high", "low", "close", "volume", "amount", "pct_chg"):
            assert np.array_equal(
                getattr(actual, field), getattr(expected, field)
            ), f"{code} 的 {field} 不一致"
        assert actual.code == expected.code


def test_names_identical(both):
    _latest, old_grouped, new = both
    for code, records in old_grouped.items():
        expected = [str(r.get("name") or "") for r in records]
        assert new.names[code] == expected, f"{code} 名称序列不一致"


def test_st_rows_identical(both):
    _latest, old_grouped, new = both
    old_days, old_stats = bp._st_rows(old_grouped)
    new_days, new_stats = bp._st_rows_from_names(new.names, new.bars)

    assert old_stats == new_stats
    assert set(old_days) == set(new_days)
    for code in old_days:
        assert old_days[code] == new_days[code], f"{code} 的 ST 日期集合不一致"


def test_selection_and_benchmark_pools_identical(both):
    _latest, old_grouped, new = both
    targets = bp._resolve_targets("new_high")

    old_pool = bp._select_bars(old_grouped, targets)
    new_pool = bp._select_bars(new.bars, targets)
    assert set(old_pool) == set(new_pool)

    old_bench = bp._benchmark_pool(old_grouped)
    new_bench = bp._benchmark_pool(new.bars)
    assert set(old_bench) == set(new_bench)


def test_overlay_applied_identically(tmp_path, db_path):
    """名称覆盖在两套结构上得到相同名称。"""
    overlay = tmp_path / "names.db"
    with sqlite3.connect(overlay) as conn:
        conn.execute(
            """
            CREATE TABLE stock_daily (
                trade_date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
                open REAL, high REAL, low REAL, close REAL,
                pct_chg REAL, volume REAL, amount REAL,
                PRIMARY KEY (trade_date, code)
            )
            """
        )
        conn.execute(
            "INSERT INTO stock_daily VALUES ('2024-01-05','000001','ST覆盖',1,1,1,1,0,1,1)"
        )

    engine = bp._readonly_engine(db_path)
    try:
        old_latest, old_grouped = bp._load_history(
            engine, ["000001", "000002"], 200, overlay_names_db=overlay
        )
        new = bp._load_history_compact(
            engine, ["000001", "000002"], 200, overlay_names_db=overlay
        )
    finally:
        engine.dispose()

    for code in old_grouped:
        expected = [str(r.get("name") or "") for r in old_grouped[code]]
        assert new.names[code] == expected, f"{code} 覆盖后名称不一致"
    assert "ST覆盖" in new.names["000001"]
