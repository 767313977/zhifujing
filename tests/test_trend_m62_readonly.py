"""M6.2 只读探针单测：mode=ro + query_only 证明与文件零变更。"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import m62_history_independence as history  # noqa: E402
import m62_readonly_probe as probe  # noqa: E402


def _seed_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE stock_daily (code TEXT, trade_date TEXT, close REAL)")
        connection.execute("INSERT INTO stock_daily VALUES ('000001', '2026-01-05', 10.0)")
        connection.commit()
    finally:
        connection.close()


def test_probe_proves_readonly(tmp_path: Path) -> None:
    db = tmp_path / "sample.db"
    _seed_db(db)
    result = probe.probe(db, with_hash=True)
    assert result["status"] == "READONLY_PROVEN"
    assert result["query_only"] == 1
    assert len(result["attempts"]) == len(probe.WRITE_STATEMENTS)
    assert all(item["blocked"] for item in result["attempts"])
    assert result["unchanged"] is True
    assert result["before_sha256"] == result["after_sha256"]
    assert result["before"]["mtime_ns"] == result["after"]["mtime_ns"]


def test_probe_does_not_create_missing_file(tmp_path: Path) -> None:
    db = tmp_path / "absent.db"
    result = probe.probe(db, with_hash=False)
    assert result["status"] == "MISSING"
    assert not db.exists()


def test_probe_marks_drop_as_noop(tmp_path: Path) -> None:
    db = tmp_path / "sample.db"
    _seed_db(db)
    result = probe.probe(db, with_hash=False)
    drop = [item for item in result["attempts"] if item["statement"] == "drop"][0]
    assert drop["effect"] == "noop"
    assert drop["error"] is None


def test_history_query_enforces_query_only(tmp_path: Path) -> None:
    db = tmp_path / "sample.db"
    _seed_db(db)
    assert history.query(db, "SELECT COUNT(*) FROM stock_daily") == [(1,)]
    with pytest.raises(sqlite3.OperationalError):
        history.query(db, "CREATE TABLE _probe (x INTEGER)")
