"""M6.3 breadth 独立实现单测。"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import m63_breadth_materialize as breadth  # noqa: E402


def _make_db(path: Path, days: int = 70) -> str:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE stock_daily (code TEXT, trade_date TEXT, close REAL, pct_chg REAL, amount REAL)")
    start = date(2025, 1, 2)
    last = None
    for index in range(days):
        day = (start + timedelta(days=index)).isoformat()
        last = day
        connection.execute("INSERT INTO stock_daily VALUES ('600001', ?, ?, 1.0, 1e8)", (day, 10.0 + index))
        connection.execute("INSERT INTO stock_daily VALUES ('600002', ?, ?, -1.0, 1e8)", (day, 10.0))
    connection.commit()
    connection.close()
    return last


def test_breadth_python_full_counts(tmp_path: Path) -> None:
    db = tmp_path / "sample.db"
    day = _make_db(db)
    connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        stats = breadth.breadth_python_full(connection, day)
    finally:
        connection.close()
    assert stats["universe"] == 2
    assert stats["up"] == 1
    assert stats["down"] == 1
    assert stats["flat"] == 0
    assert stats["ma20_denominator"] == 2
    assert stats["above_ma20"] == 1
    assert stats["ma60_denominator"] == 2
    assert stats["above_ma60"] == 1
    assert stats["high20"] == 2
    assert stats["low20"] == 1
