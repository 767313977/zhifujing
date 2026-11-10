"""factor_ic 只读保护：不得写库、不得在缺库时新建文件。

B3 整改前 `scripts/factor_ic.py` 用普通 `create_engine()`，库不存在时 SQLite 会
新建空文件；本轮改为 `mode=ro` URI。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts import factor_ic  # noqa: E402


def _make_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
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
        rows = [
            (f"2024-01-{d:02d}", "000001", "测试", 10.0, 10.2, 9.9, 10.1, 1.0, 1e6, 1e7)
            for d in range(1, 29)
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?)", rows
        )


def test_load_uses_readonly_uri(tmp_path, monkeypatch):
    db = tmp_path / "history.db"
    _make_db(db)

    opened: list[str] = []
    real = factor_ic.create_engine

    def spy(url, *args, **kwargs):
        opened.append(str(url))
        return real(url, *args, **kwargs)

    monkeypatch.setattr(factor_ic, "create_engine", spy)
    factor_ic._load(db, 100, 0)

    assert opened, "应至少打开一次数据库"
    assert all("mode=ro" in url for url in opened), opened


def test_load_missing_db_does_not_create(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(Exception):
        factor_ic._load(missing, 100, 0)
    assert not missing.exists(), "缺库时不得新建文件"


def test_load_does_not_modify_source(tmp_path):
    db = tmp_path / "history.db"
    _make_db(db)
    before = db.read_bytes()
    factor_ic._load(db, 100, 0)
    assert db.read_bytes() == before
