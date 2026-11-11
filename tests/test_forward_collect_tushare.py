"""Tushare 前瞻采集器的离线测试（全部合成数据，不联网、不碰真实库）。

覆盖：正常日、整段缺失、133 只缺失、空结果、6,000 行触顶、重复代码、
缺字段、单位错误、事务回滚、同日重复、日历缺失、收盘前、停牌不伪造、
数据版本稳定性、上一确认日不被失败批次破坏。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "backend", ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts import forward_collect_tushare as collector  # noqa: E402
from scripts import forward_store as store  # noqa: E402

DAY = date(2026, 11, 11)
PREV = date(2026, 11, 10)
NOW = datetime(2026, 11, 11, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
CAL = [PREV, DAY, date(2026, 11, 12)]


def _universe() -> set[str]:
    codes: list[str] = []
    for prefix, size in (("600", 40), ("688", 20), ("000", 60), ("300", 40), ("920", 20)):
        codes.extend(f"{prefix}{index:03d}" for index in range(size))
    return set(codes)


def _suffix(code: str) -> str:
    if code[:2] in ("43", "83", "87", "88", "92"):
        return ".BJ"
    return ".SH" if code[:2] in ("60", "68") else ".SZ"


def _quotes(codes, *, close: float = 10.0, vol: float = 1000.0, amount: float = 1000.0) -> list[dict]:
    """`vol` 单位为手、`amount` 单位为千元（Tushare 原始口径）。"""
    return [
        {
            "ts_code": f"{code}{_suffix(code)}",
            "trade_date": DAY.strftime("%Y%m%d"),
            "open": close,
            "high": round(close * 1.01, 4),
            "low": round(close * 0.99, 4),
            "close": close,
            "pct_chg": 1.0,
            "vol": vol,
            "amount": amount,
        }
        for code in sorted(codes)
    ]


def _fetch_of(payload):
    calls: list[date] = []

    def _fetch(day: date):
        calls.append(day)
        return payload(day) if callable(payload) else payload

    return _fetch, calls


def _run(db_path, codes, payload, *, day=DAY, now=NOW, calendar=CAL, execute=True,
         revise=False, min_cov=0.985):
    fetch, calls = _fetch_of(payload)
    result = collector.collect(
        db_path, day, fetch=fetch, manifest_codes=set(codes), calendar=calendar,
        now=now, execute=execute, revise=revise, min_universe_coverage=min_cov,
    )
    return result, calls


def _count(db_path: Path, sql: str, params: tuple = ()) -> int:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        return int(con.execute(sql, params).fetchone()[0])
    finally:
        con.close()


def _rows(db_path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "forward_research.db"


# ---- 正常日 -----------------------------------------------------------------


def test_normal_day_confirms_units_and_marks(db_path):
    codes = _universe()
    result, calls = _run(db_path, codes, _quotes(codes))

    assert result["status"] == "confirmed"
    assert result["exit_code"] == collector.EXIT_OK
    assert calls == [DAY]
    assert _count(db_path, "SELECT COUNT(*) FROM stock_daily WHERE trade_date=?", (DAY.isoformat(),)) == len(codes)

    row = _rows(
        db_path,
        "SELECT volume, amount, raw_code, board, source, pct_source, data_version "
        "FROM stock_daily WHERE trade_date=? AND code='600000'",
        (DAY.isoformat(),),
    )[0]
    assert row[0] == pytest.approx(1000.0 * 100)
    assert row[1] == pytest.approx(1000.0 * 1000)
    assert row[2] == "600000.SH"
    assert row[3] == "沪主板"
    assert row[4] == "tushare"
    assert row[5] == "official"
    assert row[6].startswith(f"tushare-daily:{DAY.isoformat()}:")

    # 逐券状态：成功返回的都在
    assert _count(db_path, "SELECT COUNT(*) FROM universe_status WHERE trade_date=? AND status='ok'",
                  (DAY.isoformat(),)) == len(codes)
    # 完成标记 + 全局版本都要有
    domains = {row[0] for row in _rows(db_path, "SELECT domain FROM data_revision")}
    assert f"stock_daily:{DAY.isoformat()}" in domains
    assert "stock_daily" in domains
    log = _rows(db_path, "SELECT status, rows, message FROM ingest_log ORDER BY id DESC LIMIT 1")[0]
    assert log[0] == "ok" and log[1] == len(codes)
    message = json.loads(log[2])
    assert message["unique"] == len(codes)
    assert message["by_board"]["北交所"] == 20
    assert message["data_version"] == row[6]


# ---- 整段缺失 / 覆盖不足 -----------------------------------------------------


def test_133_missing_rejected_without_writes(db_path):
    codes = _universe()
    payload = _quotes(sorted(codes)[: len(codes) - 133])
    with pytest.raises(collector.BatchRejected) as error:
        _run(db_path, codes, payload)
    assert error.value.error_type == "incomplete"
    assert _count(db_path, "SELECT COUNT(*) FROM stock_daily") == 0
    assert _count(db_path, "SELECT COUNT(*) FROM data_revision") == 0
    log = _rows(db_path, "SELECT status, message FROM ingest_log ORDER BY id DESC LIMIT 1")[0]
    assert log[0] == "failed"
    assert json.loads(log[1])["error_type"] == "incomplete"


def test_whole_000_segment_missing_rejected(db_path):
    codes = _universe()
    payload = _quotes([code for code in codes if not code.startswith("000")])
    with pytest.raises(collector.BatchRejected) as error:
        _run(db_path, codes, payload)
    assert error.value.error_type == "incomplete"
    assert any("覆盖" in item for item in error.value.problems)
    assert _count(db_path, "SELECT COUNT(*) FROM stock_daily") == 0


def test_empty_response_is_api_failure_not_all_suspended(db_path):
    with pytest.raises(collector.BatchRejected) as error:
        _run(db_path, _universe(), [])
    assert error.value.error_type == "empty"
    assert _count(db_path, "SELECT COUNT(*) FROM stock_daily") == 0
    log = _rows(db_path, "SELECT status, message FROM ingest_log ORDER BY id DESC LIMIT 1")[0]
    assert log[0] == "failed" and json.loads(log[1])["error_type"] == "empty"


def test_6000_rows_is_truncation_suspicion(db_path):
    prefixes = ("600", "601", "603", "605", "688", "000", "001", "002", "003", "300", "301", "302")
    codes = {f"{prefix}{index:03d}" for prefix in prefixes for index in range(500)}
    codes = set(sorted(codes)[:6000])
    with pytest.raises(collector.BatchRejected) as error:
        _run(db_path, codes, _quotes(codes))
    assert error.value.error_type == "incomplete"
    assert any("上限" in item for item in error.value.problems)


# ---- 行级数据问题 -----------------------------------------------------------


def test_duplicate_codes_rejected(db_path):
    codes = _universe()
    payload = _quotes(codes)
    payload.append(dict(payload[0]))
    with pytest.raises(collector.BatchRejected) as error:
        _run(db_path, codes, payload)
    assert error.value.error_type == "row_invalid"
    assert any("重复代码" in item for item in error.value.problems)


def test_missing_amount_rejected(db_path):
    codes = _universe()
    payload = _quotes(codes)
    payload[0]["amount"] = None
    with pytest.raises(collector.BatchRejected) as error:
        _run(db_path, codes, payload)
    assert any("amount" in item for item in error.value.problems)
    assert _count(db_path, "SELECT COUNT(*) FROM stock_daily") == 0


def test_unit_mismatch_rejected(db_path):
    codes = _universe()
    payload = _quotes(codes)
    # 把 vol 当成「股」传入：换算后成交量放大 100 倍，均价只剩收盘价的 1%
    payload[0]["vol"] = 1_000_000.0
    with pytest.raises(collector.BatchRejected) as error:
        _run(db_path, codes, payload)
    assert any("单位不自洽" in item for item in error.value.problems)


# ---- 停牌不伪造 -------------------------------------------------------------


def test_single_absent_stock_marked_suspended_not_fabricated(db_path):
    codes = _universe()
    absent = "000001"
    payload = _quotes([code for code in codes if code != absent])
    result, _ = _run(db_path, codes, payload, min_cov=0.99)
    assert result["status"] == "confirmed"
    assert _count(db_path, "SELECT COUNT(*) FROM stock_daily WHERE code=?", (absent,)) == 0
    status = _rows(db_path, "SELECT status, reason FROM universe_status WHERE trade_date=? AND code=?",
                   (DAY.isoformat(), absent))[0]
    assert status == ("suspended_no_quote", "absent_from_daily")


# ---- 幂等 / 事务 / 前置条件 -------------------------------------------------


def test_second_run_same_day_does_not_fetch_or_overwrite(db_path):
    codes = _universe()
    _run(db_path, codes, _quotes(codes))
    before = _rows(db_path, "SELECT volume, amount FROM stock_daily WHERE code='600000'")
    result, calls = _run(db_path, codes, _quotes(codes, close=99.0))
    assert result["status"] == "already_confirmed"
    assert result["exit_code"] == collector.EXIT_ALREADY
    assert calls == []
    assert _rows(db_path, "SELECT volume, amount FROM stock_daily WHERE code='600000'") == before


def test_write_failure_rolls_back_and_logs(db_path, monkeypatch):
    codes = _universe()

    def boom(engine, batch, *, now, revise):
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO stock_daily(trade_date, code, board, source, pct_source, ingested_at) "
                "VALUES ('2026-11-11', '600000', '沪主板', 'tushare', 'official', 'x')"
            )
            raise RuntimeError("disk full")

    monkeypatch.setattr(collector, "_write_confirmed", boom)
    with pytest.raises(RuntimeError):
        _run(db_path, codes, _quotes(codes))
    assert _count(db_path, "SELECT COUNT(*) FROM stock_daily") == 0
    assert _count(db_path, "SELECT COUNT(*) FROM data_revision") == 0
    log = _rows(db_path, "SELECT status, message FROM ingest_log ORDER BY id DESC LIMIT 1")[0]
    assert log[0] == "failed" and json.loads(log[1])["error_type"] == "db"


def test_calendar_missing_rejected_before_fetch(db_path):
    codes = _universe()
    with pytest.raises(collector.BatchRejected) as error:
        _run(db_path, codes, _quotes(codes), calendar=[PREV])
    assert error.value.error_type == "calendar"


def test_before_close_rejected(db_path):
    codes = _universe()
    with pytest.raises(collector.BatchRejected) as error:
        _run(db_path, codes, _quotes(codes), now=datetime(2026, 11, 11, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert error.value.error_type == "not_ready"


def test_plan_mode_does_not_create_db(tmp_path):
    db_path = tmp_path / "forward_research.db"
    codes = _universe()
    result, calls = _run(db_path, codes, _quotes(codes), execute=False)
    assert result["status"] == "plan"
    assert calls == []
    assert not db_path.exists()


def test_rejected_day_keeps_previous_confirmed_day(db_path):
    codes = _universe()
    _run(db_path, codes, _quotes(codes), day=PREV, now=datetime(2026, 11, 10, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    payload = _quotes([code for code in codes if not code.startswith("000")])
    with pytest.raises(collector.BatchRejected):
        _run(db_path, codes, payload, calendar=[PREV, DAY])
    assert _count(db_path, "SELECT COUNT(*) FROM stock_daily WHERE trade_date=?", (PREV.isoformat(),)) == len(codes)
    assert _count(db_path, "SELECT COUNT(*) FROM stock_daily WHERE trade_date=?", (DAY.isoformat(),)) == 0
    domains = {row[0] for row in _rows(db_path, "SELECT domain FROM data_revision")}
    assert f"stock_daily:{PREV.isoformat()}" in domains
    assert f"stock_daily:{DAY.isoformat()}" not in domains


def test_explicit_revision_bumps_version_and_replaces_rows(db_path):
    codes = _universe()
    _run(db_path, codes, _quotes(codes))
    result, _ = _run(db_path, codes, _quotes(codes, close=11.0), revise=True)
    assert result["status"] == "confirmed"
    revision = _count(db_path, "SELECT revision FROM data_revision WHERE domain=?",
                      (f"stock_daily:{DAY.isoformat()}",))
    assert revision == 2
    assert _rows(db_path, "SELECT DISTINCT close FROM stock_daily WHERE trade_date=?",
                 (DAY.isoformat(),)) == [(11.0,)]
    assert _count(db_path, "SELECT COUNT(*) FROM stock_daily WHERE trade_date=?", (DAY.isoformat(),)) == len(codes)


# ---- 版本与目标库保护 -------------------------------------------------------


def test_data_version_is_stable_and_changes_with_payload():
    rows = [
        {"code": "600000", "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0,
         "pct_chg": 0.0, "volume": 100.0, "amount": 100.0},
        {"code": "000001", "open": 2.0, "high": 2.1, "low": 1.9, "close": 2.0,
         "pct_chg": -1.0, "volume": 200.0, "amount": 400.0},
    ]
    first = collector.batch_digest(rows)
    assert first == collector.batch_digest(list(reversed(rows)))
    changed = [dict(row) for row in rows]
    changed[0]["close"] = 1.01
    assert collector.batch_digest(changed) != first


@pytest.mark.parametrize("name", ["history.db", "fupan.db", "b1_research.db"])
def test_forbidden_target_databases(tmp_path, name):
    with pytest.raises(SystemExit):
        collector._check_target(tmp_path / name)


def test_store_schema_has_universe_status(tmp_path):
    engine = store.connect(tmp_path / "forward_research.db")
    store.create_schema(engine)
    with engine.begin() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(universe_status)")}
    engine.dispose()
    assert columns == {"trade_date", "code", "status", "reason"}
