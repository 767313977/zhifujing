"""M6.2 研究工具单测：WF 区块、日期聚类与证据追溯工具。"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import m62_catalog_data as catalog  # noqa: E402
import m62_market_data_audit as market  # noqa: E402
import m62_reconcile_m61_evidence as recon  # noqa: E402
import m62_trade_sim_audit as sim_audit  # noqa: E402

from app.services.trend_engine import trade_sim  # noqa: E402


def _catalog_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE stock_daily (code TEXT, trade_date TEXT, close REAL)")
        connection.execute("CREATE TABLE rps_daily (code TEXT, trade_date TEXT, ret20 REAL, updated_at TEXT)")
        connection.execute("INSERT INTO stock_daily VALUES ('000001', '2026-01-05', 10.0)")
        connection.execute("INSERT INTO stock_daily VALUES ('000001', '2026-01-06', 10.5)")
        connection.execute("CREATE INDEX idx_stock_daily_date ON stock_daily (trade_date)")
        connection.commit()
    finally:
        connection.close()


def test_table_profile_reports_range_and_pit_columns(tmp_path: Path) -> None:
    db = tmp_path / "sample.db"
    _catalog_db(db)
    connection = catalog.open_ro(db)
    try:
        profile = catalog.table_profile(connection, "rps_daily")
    finally:
        connection.close()
    assert profile["rows"] == 0
    assert profile["date_range"] == {"column": "trade_date", "min": None, "max": None, "distinct": 0}
    assert profile["pit_columns"] == ["updated_at"]
    assert profile["has_ingest_time"] is True


def test_database_profile_lists_tables_and_indexes(tmp_path: Path) -> None:
    db = tmp_path / "sample.db"
    _catalog_db(db)
    profile = catalog.database_profile(db)
    tables = {item["table"] for item in profile["tables"]}
    assert tables == {"stock_daily", "rps_daily"}
    assert any(index["name"] == "idx_stock_daily_date" for index in profile["indexes"])
    stock = [item for item in profile["tables"] if item["table"] == "stock_daily"][0]
    assert stock["date_range"]["distinct"] == 2
    assert stock["has_ingest_time"] is False


def test_limit_threshold_by_board() -> None:
    assert market.limit_threshold("300001") == 19.8
    assert market.limit_threshold("688001") == 19.8
    assert market.limit_threshold("830001") == 29.8
    assert market.limit_threshold("600000") == 9.8


def test_breadth_aggregate_counts_and_denominators() -> None:
    rows = [
        # code, close, pct_chg, amount, ma20, ma60, high20, low20, high60, low60, n20, n60
        ("300001", 11.0, 19.9, 1e9, 10.0, 9.0, 11.0, 8.0, 12.0, 7.0, 20, 60),
        ("600000", 9.0, -0.5, 5e8, 10.0, 9.5, 12.0, 8.9, 13.0, 8.5, 20, 60),
        ("000001", 10.0, 0.0, 1e8, 10.0, 10.0, 10.5, 9.0, 11.0, 8.0, 10, 30),
        ("000002", 5.0, 1.0, 0.0, None, None, 5.5, 4.0, 6.0, 3.0, 20, 60),
    ]
    stats = market.aggregate(rows)
    assert stats["universe"] == 3
    assert stats["up"] == 1
    assert stats["down"] == 1
    assert stats["flat"] == 1
    assert stats["ma20_denominator"] == 2
    assert stats["above_ma20"] == 1
    assert stats["high20"] == 1
    assert stats["low20"] == 0
    assert stats["limit_up_approx"] == 1
    assert stats["limit_down_approx"] == 0


def test_sim_audit_check_accepts_balanced_ledger() -> None:
    days = [date(2025, 1, 2).toordinal() + index for index in range(10)]
    days = [date.fromordinal(value) for value in days]
    bars = {
        "600001": {
            day: trade_sim.DailyBar(
                open=10.0, high=10.1, low=9.9, close=10.0, pct=0.0, amount=1e8, volume=1e6
            )
            for day in days
        }
    }
    config = trade_sim.SimConfig(horizon=2, min_commission=0, commission_bps=3)
    result = trade_sim.simulate(
        bars_by_code=bars,
        calendar=days,
        signals=[trade_sim.Signal("600001", days[0], 1.0)],
        config=config,
    )
    checks = sim_audit.check(result, config)
    assert checks["ledger_identity_max_gap"] == 0
    assert checks["invariants_ok"] is True
    assert checks["open_positions"] == 0
    assert abs(checks["accounting_gap"]) <= 0.05


def test_purged_blocks_generates_three_test_blocks() -> None:
    days = [date(2026, 1, 1).toordinal() + index for index in range(100)]
    days = [date.fromordinal(value) for value in days]
    blocks = recon.exp.purged_blocks(days, blocks=4, embargo=20)
    assert len(blocks) == 3
    assert blocks[0]["test_start"] == str(days[25])
    assert blocks[0]["test_end"] == str(days[49])
    assert blocks[0]["embargo_start"] == str(days[5])
    assert blocks[-1]["test_end"] == str(days[-1])
    assert blocks[0]["test_days"] == days[25:50]


def test_compare_reports_differences() -> None:
    expected = {"days": 3, "mean": 0.554, "ci_low": 0.332, "ci_high": 0.67}
    same = recon.compare(expected, dict(expected))
    assert same["match"] is True
    worse = dict(expected, ci_low=0.1)
    diff = recon.compare(expected, worse)
    assert diff["match"] is False
    assert diff["differences"]["ci_low"] == {"expected": 0.332, "reproduced": 0.1}


def _sample(day: str, code: str, excess: float = 1.0, horizon: int = 20, exit_day: str | None = None):
    return SimpleNamespace(
        horizon=horizon,
        status="ok",
        excess=excess,
        signal_date=date.fromisoformat(day),
        code=code,
        exit_date=date.fromisoformat(exit_day) if exit_day else None,
    )


def test_describe_days_clusters_by_signal_date() -> None:
    samples = [
        _sample("2026-08-18", "000001"),
        _sample("2026-08-18", "000002"),
        _sample("2026-09-01", "000003"),
        _sample("2026-09-01", "000003"),  # 重复信号
        _sample("2026-09-01", "000004", horizon=60),
    ]
    stats = recon.describe_days(samples)
    assert stats["samples"] == 4
    assert stats["distinct_days"] == 2
    assert stats["per_day_counts"] == {"2026-08-18": 2, "2026-09-01": 2}
    assert stats["duplicate_signal_pairs"] == 1
    assert stats["max_per_day"] == 2


def test_label_overflow_counts_samples_past_block_end() -> None:
    samples = [
        _sample("2026-09-01", "000001", exit_day="2026-09-30"),
        _sample("2026-09-02", "000002", exit_day="2026-10-10"),
    ]
    assert recon.label_overflow(samples, date(2026, 10, 9), 20) == 1
    assert recon.label_overflow(samples, date(2026, 10, 9), 60) == 0
