"""Tushare 前瞻采集器与正式评价审计的离线测试（合成数据，不联网、不碰真实库）。

覆盖：上市日期进入分市场基线、上一确认日同口径、单事务故障注入、修订禁用、
路径保护、日历初始化与 40 日预检、正式评价的版本/日志/内容哈希审计。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session as OrmSession

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "backend", ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts import forward_collect_tushare as collector  # noqa: E402
from scripts import forward_store as store  # noqa: E402
from scripts import forward_track_c0 as ft  # noqa: E402

CST = ZoneInfo("Asia/Shanghai")
DAY = date(2026, 11, 11)
PREV = date(2026, 11, 10)
NEXT = date(2026, 11, 12)
NOW = datetime(2026, 11, 11, 16, 0, tzinfo=CST)
CAL = [PREV, DAY, NEXT]

#: 与真实证据一致的 5 只「窗口内新上市」北交所证券（其余 27 只在窗口前已上市）
BSE_DATES = {
    "920027": "2026-09-16",
    "920028": "2026-09-22",
    "920029": "2026-09-23",
    "920030": "2026-09-24",
    "920031": "2026-09-29",
}


# ---------------------------------------------------------------------------
# 合成数据
# ---------------------------------------------------------------------------


def _universe() -> set[str]:
    codes: list[str] = []
    for prefix, size in (("600", 40), ("688", 20), ("000", 60), ("300", 40), ("920", 32)):
        codes.extend(f"{prefix}{index:03d}" for index in range(size))
    return set(codes)


def _big_universe() -> set[str]:
    codes: list[str] = []
    for prefix, size in (("600", 400), ("000", 400), ("300", 140), ("688", 40), ("920", 20)):
        codes.extend(f"{prefix}{index:03d}" for index in range(size))
    return set(codes)


def _manifest(
    codes: set[str] | None = None,
    *,
    version: str = "test-v1",
    listing_dates: dict[str, str] | None = None,
    first_dates: dict[str, str] | None = None,
) -> collector.Manifest:
    chosen = set(codes or _universe())
    counts: dict[str, int] = {}
    for code in chosen:
        board = store.board_of(code)
        counts[board] = counts.get(board, 0) + 1
    first = {code: "2021-01-01" for code in chosen}
    first.update(first_dates or {})
    return collector.Manifest(
        version=version,
        eligibility_sha256="test-eligibility",
        universe_sha256="test-universe",
        data_cutoff="2026-10-19",
        codes=frozenset(chosen),
        board_counts=counts,
        total=len(chosen),
        listing_dates=dict(listing_dates or {}),
        first_dates=first,
    )


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


def _calendar_db(tmp_path: Path, days: list[date]) -> Path:
    path = tmp_path / "source_calendar.db"
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS trade_calendar (trade_date TEXT PRIMARY KEY)")
        con.executemany(
            "INSERT OR IGNORE INTO trade_calendar VALUES (?)", [(item.isoformat(),) for item in days]
        )
        con.commit()
    finally:
        con.close()
    return path


def _init_target(db_path: Path, tmp_path: Path, *, days: list[date], now: datetime, end: date | None = None):
    source = _calendar_db(tmp_path, days)
    collector.init_calendar(
        db_path,
        source_db=source,
        start=min(days),
        end=end or max(days),
        now=now,
        allowed_root=tmp_path,
    )


def _fetch_of(payload):
    calls: list[date] = []

    def _fetch(day: date):
        calls.append(day)
        return payload(day) if callable(payload) else payload

    return _fetch, calls


def _run(
    db_path,
    tmp_path,
    manifest,
    payload,
    *,
    day=DAY,
    now=NOW,
    calendar=CAL,
    execute=True,
    revise=False,
    reason="",
    suspended=None,
    purpose="live",
    min_cov=0.985,
):
    fetch, calls = _fetch_of(payload)
    result = collector.collect(
        db_path,
        day,
        fetch=fetch,
        manifest=manifest,
        calendar=calendar,
        now=now,
        execute=execute,
        revise=revise,
        revise_reason=reason,
        suspended_reasons=suspended,
        purpose=purpose,
        min_universe_coverage=min_cov,
        allowed_root=tmp_path,
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


def _raw_manifest(
    tmp_path: Path,
    codes: set[str],
    *,
    version: str = "v-test",
    official: dict[str, str | None] | None = None,
) -> tuple[Path, str]:
    """按 `forward_universe.build_manifest` 的哈希口径写一份合成清单。"""
    official = official or {}
    ordered = [
        {
            "code": code,
            "name": f"测试{code}",
            "board": store.board_of(code),
            "first_date": "2021-01-01",
            "forward_eligible": True,
            "identity_verified": True,
            "official_listing_date": official.get(code),
            "official_source": "test" if official.get(code) else None,
        }
        for code in sorted(codes)
    ]
    eligibility = "\n".join(f"{item['code']}|1" for item in ordered)
    universe = "\n".join(f"{item['code']}|{item['name']}|{item['board']}" for item in ordered)
    counts: dict[str, int] = {}
    for item in ordered:
        counts[item["board"]] = counts.get(item["board"], 0) + 1
    payload = {
        "version": version,
        "data_cutoff": "2026-10-19",
        "universe_sha256": hashlib.sha256(universe.encode("utf-8")).hexdigest(),
        "eligibility_sha256": hashlib.sha256(eligibility.encode("utf-8")).hexdigest(),
        "counts": {
            "total": len(ordered),
            "forward_eligible": len(ordered),
            "excluded_unverified": 0,
            "by_board": counts,
        },
        "securities": ordered,
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path, payload["eligibility_sha256"]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "forward_research.db"


# ---------------------------------------------------------------------------
# 清单与上市日期证据
# ---------------------------------------------------------------------------


def test_real_frozen_manifest_has_eligible_and_listing_metadata():
    manifest = collector.load_manifest(collector.DEFAULT_MANIFEST)
    assert manifest.version == "b1v-2026-11-11"
    assert manifest.total == 5108
    assert manifest.board_counts["北交所"] == 32
    assert manifest.listing_dates["920025"] == "2026-09-23"
    assert manifest.listing_dates["920202"] == "2026-09-29"
    assert len(manifest.listing_dates) == 32
    listed_0909, not_listed, _ = manifest.split_by_listing(date(2026, 9, 9))
    assert sum(1 for code in listed_0909 if code.startswith("92")) == 27
    assert len([code for code in not_listed if code.startswith("92")]) == 5


def test_manifest_version_mismatch_rejected(tmp_path):
    path, sha = _raw_manifest(tmp_path, _universe())
    with pytest.raises(SystemExit):
        collector.load_manifest(path, expect_version="v-other", expect_eligibility_sha256=sha)


def test_manifest_eligibility_hash_mismatch_rejected(tmp_path):
    path, sha = _raw_manifest(tmp_path, _universe())
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["securities"][0]["forward_eligible"] = False
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit):
        collector.load_manifest(path, expect_version="v-test", expect_eligibility_sha256=sha)


def test_manifest_rejects_unverified_eligible(tmp_path):
    path, _sha = _raw_manifest(tmp_path, _universe())
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["securities"][0]["identity_verified"] = False
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit):
        collector.load_manifest(path, expect_version="v-test", expect_eligibility_sha256="")


def test_manifest_rejects_bse_without_listing_date(tmp_path):
    codes = {"920025", "600000"}
    path, sha = _raw_manifest(tmp_path, codes, official={"920025": None})
    with pytest.raises(SystemExit) as error:
        collector.load_manifest(path, expect_version="v-test", expect_eligibility_sha256=sha)
    assert "上市日期" in str(error.value)


def test_manifest_rejects_listing_date_conflict_with_evidence(tmp_path):
    codes = {"920025", "600000"}
    path, sha = _raw_manifest(tmp_path, codes, official={"920025": "2020-01-01"})
    with pytest.raises(SystemExit) as error:
        collector.load_manifest(path, expect_version="v-test", expect_eligibility_sha256=sha)
    assert "不一致" in str(error.value)


# ---------------------------------------------------------------------------
# 正常日 / 幂等 / 清单外证券
# ---------------------------------------------------------------------------


def test_normal_day_confirms_atomic_pieces_and_message(db_path, tmp_path):
    codes = _universe()
    manifest = _manifest(codes)
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    result, calls = _run(db_path, tmp_path, manifest, _quotes(codes))

    assert result["status"] == "confirmed"
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
    assert (row[4], row[5]) == ("tushare", "official")
    assert row[6].endswith(":r1")

    assert _count(db_path, "SELECT COUNT(*) FROM universe_status WHERE status='ok'") == len(codes)
    assert _count(db_path, "SELECT COUNT(*) FROM data_revision WHERE domain=?", (f"stock_daily:{DAY}",)) == 1
    assert _count(db_path, "SELECT COUNT(*) FROM ingest_log WHERE status='ok'") == 1
    message = json.loads(_rows(db_path, "SELECT message FROM ingest_log WHERE status='ok'")[0][0])
    assert message["revision"] == 1 and message["purpose"] == "live"
    assert message["expected_total"] == len(codes)
    assert message["expected_by_board"]["北交所"] == 32
    assert message["data_protocol"] == store.DATA_PROTOCOL_VERSION
    assert message["listing_protocol"] == store.LISTING_PROTOCOL_VERSION


def test_second_run_same_day_does_not_fetch_or_overwrite(db_path, tmp_path):
    codes = _universe()
    manifest = _manifest(codes)
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    _run(db_path, tmp_path, manifest, _quotes(codes))
    before = _rows(db_path, "SELECT volume, amount, data_version FROM stock_daily WHERE code='600000'")
    result, calls = _run(db_path, tmp_path, manifest, _quotes(codes, close=99.0))
    assert result["status"] == "already_confirmed"
    assert calls == []
    assert _rows(db_path, "SELECT volume, amount, data_version FROM stock_daily WHERE code='600000'") == before


def test_extra_codes_are_stored_but_not_counted(db_path, tmp_path):
    codes = _universe()
    manifest = _manifest(codes)
    payload = _quotes(codes) + _quotes({"601999"})
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    result, _ = _run(db_path, tmp_path, manifest, payload)
    assert result["status"] == "confirmed"
    assert _count(db_path, "SELECT COUNT(*) FROM stock_daily WHERE code='601999'") == 1
    message = json.loads(_rows(db_path, "SELECT message FROM ingest_log WHERE status='ok'")[0][0])
    assert message["extras"] == 1
    assert message["by_board_listed"].get("沪主板") == 40


# ---------------------------------------------------------------------------
# 分市场基线：首次 / 上一确认日 / 上市日期
# ---------------------------------------------------------------------------


def test_first_collect_uses_manifest_board_baseline(db_path, tmp_path):
    codes = _big_universe()
    manifest = _manifest(codes)
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    result, _ = _run(db_path, tmp_path, manifest, _quotes(codes), min_cov=collector.MIN_UNIVERSE_COVERAGE_FLOOR)
    assert result["status"] == "confirmed"


def test_first_collect_rejects_entire_bse_missing(db_path, tmp_path):
    codes = _big_universe()
    manifest = _manifest(codes)
    payload = _quotes([code for code in codes if not code.startswith("920")])
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    with pytest.raises(collector.BatchRejected) as error:
        _run(db_path, tmp_path, manifest, payload, min_cov=collector.MIN_UNIVERSE_COVERAGE_FLOOR)
    assert any("北交所" in item for item in error.value.problems)
    assert _count(db_path, "SELECT COUNT(*) FROM stock_daily") == 0


def test_first_collect_rejects_133_shenzhen_missing(db_path, tmp_path):
    codes = _big_universe()
    manifest = _manifest(codes)
    dropped = {code for code in codes if code.startswith("000")} - {f"000{index:03d}" for index in range(267)}
    payload = _quotes(codes - dropped)
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    assert len(dropped) == 133
    with pytest.raises(collector.BatchRejected) as error:
        _run(db_path, tmp_path, manifest, payload, min_cov=collector.MIN_UNIVERSE_COVERAGE_FLOOR)
    assert any("深主板" in item for item in error.value.problems)


def test_previous_day_uses_same_frozen_pool_definition(db_path, tmp_path):
    codes = _big_universe()
    extras = {f"601{index:03d}" for index in range(467)}
    manifest = _manifest(codes)
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    _run(
        db_path, tmp_path, manifest, _quotes(codes | extras),
        day=PREV, purpose="backfill", min_cov=collector.MIN_UNIVERSE_COVERAGE_FLOOR,
    )
    # 第二日：清单外 467 只仍在，冻结池覆盖正常 → 必须通过（不能拿全市场数量比池内）
    result, _ = _run(
        db_path, tmp_path, manifest, _quotes(codes | extras),
        min_cov=collector.MIN_UNIVERSE_COVERAGE_FLOOR,
    )
    assert result["status"] == "confirmed"

    # 第二日：日线写入前先清空（新库）无法复用，这里另建一天验证真实丢失 133 只被拒
    db2 = tmp_path / "second" / "forward_research.db"
    db2.parent.mkdir()
    _init_target(db2, db2.parent, days=CAL, now=NOW)
    _run(db2, db2.parent, manifest, _quotes(codes | extras), day=PREV,
         purpose="backfill", min_cov=collector.MIN_UNIVERSE_COVERAGE_FLOOR)
    dropped = {f"000{index:03d}" for index in range(133)}
    with pytest.raises(collector.BatchRejected) as error:
        _run(db2, db2.parent, manifest, _quotes((codes - dropped) | extras),
             min_cov=collector.MIN_UNIVERSE_COVERAGE_FLOOR)
    assert any("深主板" in item for item in error.value.problems)


def test_previous_day_board_drop_is_rejected(db_path, tmp_path):
    codes = _big_universe()
    manifest = _manifest(codes)
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    _run(db_path, tmp_path, manifest, _quotes(codes), day=PREV,
         purpose="backfill", min_cov=collector.MIN_UNIVERSE_COVERAGE_FLOOR)
    dropped = [f"000{index:03d}" for index in range(30)]
    with pytest.raises(collector.BatchRejected) as error:
        _run(db_path, tmp_path, manifest, _quotes(codes - set(dropped)),
             min_cov=collector.MIN_UNIVERSE_COVERAGE_FLOOR)
    assert any("上一确认日" in item and "深主板" in item for item in error.value.problems)


def _bse_manifest() -> collector.Manifest:
    codes = _universe()
    return _manifest(codes, listing_dates=dict(BSE_DATES))


@pytest.mark.parametrize(
    ("day", "expected"),
    [("2026-09-09", 27), ("2026-09-16", 28), ("2026-09-24", 31), ("2026-09-30", 32)],
)
def test_bse_expected_counts_follow_listing_dates(tmp_path, day, expected):
    codes = _universe()
    manifest = _bse_manifest()
    target_day = date.fromisoformat(day)
    root = tmp_path / day.replace("-", "")
    root.mkdir()
    db = root / "forward_research.db"
    calendar = [target_day - timedelta(days=1), target_day, target_day + timedelta(days=1)]
    _init_target(db, root, days=calendar, now=datetime.combine(target_day, datetime.min.time(), CST).replace(hour=16))
    late = {code for code, listed in BSE_DATES.items() if listed > target_day.isoformat()}
    payload = _quotes(codes - late)
    result, _ = _run(
        db, root, manifest, payload,
        day=target_day, calendar=calendar, purpose="backfill", now=NOW,
        min_cov=collector.MIN_UNIVERSE_COVERAGE_FLOOR,
    )
    assert result["status"] == "confirmed"
    message = json.loads(_rows(db, "SELECT message FROM ingest_log WHERE status='ok'")[0][0])
    assert message["expected_by_board"]["北交所"] == expected


def test_pre_listing_absence_is_not_suspension(tmp_path):
    codes = _universe()
    manifest = _bse_manifest()
    target_day = date(2026, 9, 9)
    root = tmp_path / "prelisting"
    root.mkdir()
    db = root / "forward_research.db"
    calendar = [target_day, target_day + timedelta(days=1)]
    _init_target(db, root, days=calendar, now=datetime.combine(target_day, datetime.min.time(), CST).replace(hour=16))
    payload = _quotes([code for code in codes if code not in BSE_DATES])
    result, _ = _run(
        db, root, manifest, payload, day=target_day, calendar=calendar,
        purpose="backfill", min_cov=collector.MIN_UNIVERSE_COVERAGE_FLOOR,
    )
    assert result["status"] == "confirmed"
    statuses = dict(_rows(db, "SELECT code, status FROM universe_status WHERE trade_date=?", (target_day.isoformat(),)))
    for code in BSE_DATES:
        assert statuses[code] == collector.STATUS_NOT_LISTED


def test_new_listing_enters_baseline_on_its_listing_day(tmp_path):
    manifest = _bse_manifest()
    root = tmp_path / "newlisting"
    root.mkdir()
    db = root / "forward_research.db"
    dates = [date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17)]
    _init_target(
        db, root, days=dates,
        now=datetime.combine(date(2026, 9, 16), datetime.min.time(), CST).replace(hour=16),
    )
    _run(
        db, root, manifest, _quotes(manifest.split_by_listing(dates[0])[0]),
        day=dates[0], calendar=dates, purpose="backfill",
        min_cov=collector.MIN_UNIVERSE_COVERAGE_FLOOR,
    )
    assert _rows(db, "SELECT status FROM universe_status WHERE code='920027'")[0][0] == collector.STATUS_NOT_LISTED
    _run(
        db, root, manifest, _quotes(manifest.split_by_listing(dates[1])[0]),
        day=dates[1], calendar=dates, purpose="backfill",
        min_cov=collector.MIN_UNIVERSE_COVERAGE_FLOOR,
    )
    assert _rows(
        db, "SELECT status FROM universe_status WHERE trade_date=? AND code='920027'",
        (dates[1].isoformat(),),
    )[0][0] == collector.STATUS_OK


def test_post_listing_suspension_and_whole_segment_missing(tmp_path):
    codes = _universe()
    manifest = _bse_manifest()
    target_day = date(2026, 9, 30)
    root = tmp_path / "postlisting"
    root.mkdir()
    db = root / "forward_research.db"
    calendar = [target_day, target_day + timedelta(days=1)]
    _init_target(db, root, days=calendar, now=datetime.combine(target_day, datetime.min.time(), CST).replace(hour=16))

    payload = _quotes([code for code in codes if code != "920031"])
    _run(
        db, root, manifest, payload, day=target_day, calendar=calendar,
        purpose="backfill", suspended={"920031": "交易所停牌公告"},
        min_cov=collector.MIN_UNIVERSE_COVERAGE_FLOOR,
    )
    status = _rows(db, "SELECT status, reason FROM universe_status WHERE code='920031'")[0]
    assert status == (collector.STATUS_SUSPENDED, "交易所停牌公告")

    root2 = tmp_path / "postlisting2"
    root2.mkdir()
    db2 = root2 / "forward_research.db"
    _init_target(db2, root2, days=calendar, now=datetime.combine(target_day, datetime.min.time(), CST).replace(hour=16))
    with pytest.raises(collector.BatchRejected):
        _run(
            db2, root2, manifest, _quotes([code for code in codes if not code.startswith("920")]),
            day=target_day, calendar=calendar, purpose="backfill",
            min_cov=collector.MIN_UNIVERSE_COVERAGE_FLOOR,
        )


def test_non_bse_without_official_date_is_marked_unverified(tmp_path):
    codes = _universe() | {"301999"}
    manifest = _manifest(codes, first_dates={"301999": "2026-10-01"})
    target_day = date(2026, 9, 30)
    root = tmp_path / "unverified"
    root.mkdir()
    db = root / "forward_research.db"
    calendar = [target_day, target_day + timedelta(days=1)]
    _init_target(db, root, days=calendar, now=datetime.combine(target_day, datetime.min.time(), CST).replace(hour=16))
    result, _ = _run(
        db, root, manifest, _quotes([code for code in codes if code != "301999"]),
        day=target_day, calendar=calendar, purpose="backfill", now=NOW,
        min_cov=collector.MIN_UNIVERSE_COVERAGE_FLOOR,
    )
    assert result["status"] == "confirmed"
    status = _rows(db, "SELECT status FROM universe_status WHERE code='301999'")[0][0]
    assert status == collector.STATUS_LISTING_UNVERIFIED


def test_returned_before_listing_is_rejected(tmp_path):
    codes = _universe()
    manifest = _bse_manifest()
    target_day = date(2026, 9, 9)
    root = tmp_path / "listingconflict"
    root.mkdir()
    db = root / "forward_research.db"
    calendar = [target_day, target_day + timedelta(days=1)]
    _init_target(db, root, days=calendar, now=datetime.combine(target_day, datetime.min.time(), CST).replace(hour=16))
    with pytest.raises(collector.BatchRejected) as error:
        _run(
            db, root, manifest, _quotes(codes), day=target_day, calendar=calendar,
            purpose="backfill", min_cov=collector.MIN_UNIVERSE_COVERAGE_FLOOR,
        )
    assert error.value.error_type == "listing_conflict"


# ---------------------------------------------------------------------------
# 单事务故障注入
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", ["rows", "status", "revision", "ok_log", "before_commit"])
def test_failure_at_any_stage_leaves_no_formal_state(db_path, tmp_path, monkeypatch, stage):
    codes = _universe()
    manifest = _manifest(codes)
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    real = collector._write_confirmed

    def injected(engine, batch, **kwargs):
        return real(engine, batch, _failpoint=stage, **kwargs)

    monkeypatch.setattr(collector, "_write_confirmed", injected)
    with pytest.raises(RuntimeError):
        _run(db_path, tmp_path, manifest, _quotes(codes))

    assert _count(db_path, "SELECT COUNT(*) FROM stock_daily") == 0
    assert _count(db_path, "SELECT COUNT(*) FROM universe_status") == 0
    assert _count(db_path, "SELECT COUNT(*) FROM data_revision") == 0
    assert _count(db_path, "SELECT COUNT(*) FROM ingest_log WHERE status='ok'") == 0
    failed = _rows(db_path, "SELECT message FROM ingest_log WHERE status='failed'")
    assert failed and json.loads(failed[-1][0])["error_type"] == "db"


def test_success_log_and_revision_are_never_split(db_path, tmp_path):
    codes = _universe()
    manifest = _manifest(codes)
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    _run(db_path, tmp_path, manifest, _quotes(codes))
    ok_days = {row[0] for row in _rows(db_path, "SELECT trade_date FROM ingest_log WHERE status='ok'")}
    revision_days = {
        row[0].split(":", 1)[1]
        for row in _rows(db_path, "SELECT domain FROM data_revision WHERE domain LIKE 'stock_daily:%'")
    }
    assert ok_days == revision_days == {DAY.isoformat()}


# ---------------------------------------------------------------------------
# 修订禁用
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", [DAY, PREV, date(2026, 10, 22), date(2026, 10, 14)])
def test_any_revision_is_rejected(db_path, tmp_path, target):
    codes = _universe()
    manifest = _manifest(codes)
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    with pytest.raises(collector.BatchRejected) as error:
        _run(db_path, tmp_path, manifest, _quotes(codes), day=target, revise=True, reason="尝试修订")
    assert error.value.error_type == "revise_disabled"


def test_revision_rejected_even_if_snapshot_dir_missing(db_path, tmp_path):
    """CLI 已不提供 --snapshot-dir；任何修订都失败关闭。"""
    codes = _universe()
    manifest = _manifest(codes)
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    _run(db_path, tmp_path, manifest, _quotes(codes))
    with pytest.raises(collector.BatchRejected) as error:
        _run(db_path, tmp_path, manifest, _quotes(codes, close=11.0), revise=True, reason="x")
    assert error.value.error_type == "revise_disabled"
    assert _rows(db_path, "SELECT DISTINCT close FROM stock_daily") == [(10.0,)]


def test_cli_has_no_snapshot_dir_option():
    with pytest.raises(SystemExit):
        collector.main(["--date", "2026-11-11", "--snapshot-dir", "x", "--execute"])


# ---------------------------------------------------------------------------
# 路径保护
# ---------------------------------------------------------------------------


def test_target_path_allows_relative_and_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    collector._check_target(Path("forward_research.db"), allowed_root=tmp_path)
    collector._check_target(tmp_path / "forward_research.db", allowed_root=tmp_path)


def test_target_path_rejects_escape_and_wrong_name(tmp_path):
    with pytest.raises(SystemExit):
        collector._check_target(tmp_path.parent / "forward_research.db", allowed_root=tmp_path)
    with pytest.raises(SystemExit):
        collector._check_target(tmp_path / "history.db", allowed_root=tmp_path)


def test_target_path_rejects_business_database_alias(tmp_path):
    path = tmp_path / "forward_research.db"
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE stock_universe (code TEXT)")
        con.commit()
    finally:
        con.close()
    with pytest.raises(SystemExit):
        collector._check_target(path, allowed_root=tmp_path)


def test_target_path_rejects_hardlink(tmp_path):
    real = tmp_path / "real.db"
    con = sqlite3.connect(real)
    con.execute("CREATE TABLE stock_daily (trade_date TEXT, code TEXT)")
    con.commit()
    con.close()
    link = tmp_path / "forward_research.db"
    try:
        os.link(real, link)
    except OSError:
        pytest.skip("当前文件系统不支持硬链接")
    with pytest.raises(SystemExit):
        collector._check_target(link, allowed_root=tmp_path)


def test_target_path_rejects_symlink(tmp_path):
    real = tmp_path / "real.db"
    con = sqlite3.connect(real)
    con.execute("CREATE TABLE stock_daily (trade_date TEXT, code TEXT)")
    con.commit()
    con.close()
    link = tmp_path / "forward_research.db"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")
    with pytest.raises(SystemExit):
        collector._check_target(link, allowed_root=tmp_path)


# ---------------------------------------------------------------------------
# 日历
# ---------------------------------------------------------------------------


def test_init_calendar_writes_target_only(db_path, tmp_path):
    source = _calendar_db(tmp_path, CAL)
    result = collector.init_calendar(
        db_path, source_db=source, start=PREV, end=NEXT, now=NOW, allowed_root=tmp_path
    )
    assert result["days"] == 3
    assert result["next_trade_day"] == NEXT.isoformat()
    assert _count(db_path, "SELECT COUNT(*) FROM trade_calendar") == 3
    assert _count(source, "SELECT COUNT(*) FROM trade_calendar") == 3


def test_init_calendar_rejects_when_no_next_trade_day(db_path, tmp_path):
    source = _calendar_db(tmp_path, [PREV, DAY])
    with pytest.raises(collector.BatchRejected):
        collector.init_calendar(
            db_path, source_db=source, start=PREV, end=DAY, now=NOW, allowed_root=tmp_path
        )


def test_init_calendar_rejects_when_recent_day_missing(db_path, tmp_path):
    source = _calendar_db(tmp_path, [NEXT])
    with pytest.raises(collector.BatchRejected) as error:
        collector.init_calendar(
            db_path, source_db=source, start=NEXT, end=NEXT, now=NOW, allowed_root=tmp_path
        )
    assert "最近交易日" in str(error.value)


def test_collect_requires_target_calendar(db_path, tmp_path):
    codes = _universe()
    manifest = _manifest(codes)
    fetch, calls = _fetch_of(_quotes(codes))
    with pytest.raises(collector.BatchRejected) as error:
        collector.collect(
            db_path, DAY, fetch=fetch, manifest=manifest, calendar=CAL, now=NOW,
            execute=True, allowed_root=tmp_path,
        )
    assert error.value.error_type == "calendar"
    assert calls == []


def _forty_day_fixture(tmp_path: Path):
    codes: set[str] = set()
    for prefix, size in (("600", 40), ("000", 40), ("300", 10), ("688", 5), ("920", 5)):
        codes.update(f"{prefix}{index:03d}" for index in range(size))
    manifest = _manifest(codes)
    days: list[date] = []
    cursor = date(2026, 9, 1)
    while len(days) < 40:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    last = days[-1]
    next_day = last + timedelta(days=3)
    all_days = days + [next_day]
    db = tmp_path / "forward_research.db"
    _init_target(
        db, tmp_path, days=all_days,
        now=datetime.combine(last, datetime.min.time(), CST).replace(hour=16),
        end=next_day,
    )
    collect_now = datetime.combine(last + timedelta(days=1), datetime.min.time(), CST).replace(hour=16)
    for item in days:
        _run(
            db, tmp_path, manifest, _quotes(codes), day=item, now=collect_now,
            purpose="backfill", calendar=all_days,
        )
    return db, manifest, days, next_day


def test_forty_days_plus_calendar_passes_forward_preflight(tmp_path):
    db, _manifest_obj, days, _next_day = _forty_day_fixture(tmp_path)
    last = days[-1]
    engine = store.connect_readonly(db)
    try:
        with OrmSession(engine) as session:
            problems = ft.forward_problems(
                session, last, datetime.combine(last, datetime.min.time(), CST).replace(hour=15, minute=45)
            )
    finally:
        engine.dispose()
    assert problems == []


# ---------------------------------------------------------------------------
# 其它不变量
# ---------------------------------------------------------------------------


def test_coverage_floor_cannot_be_disabled(db_path, tmp_path):
    codes = _universe()
    manifest = _manifest(codes)
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    fetch, calls = _fetch_of(_quotes(codes))
    with pytest.raises(collector.BatchRejected) as error:
        collector.collect(
            db_path, DAY, fetch=fetch, manifest=manifest, calendar=CAL, now=NOW,
            execute=True, min_universe_coverage=0.0, allowed_root=tmp_path,
        )
    assert error.value.error_type == "config"
    assert calls == []


def test_purpose_separates_live_and_backfill(db_path, tmp_path):
    codes = _universe()
    manifest = _manifest(codes)
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    with pytest.raises(collector.BatchRejected) as error:
        _run(db_path, tmp_path, manifest, _quotes(codes), day=PREV, purpose="live")
    assert error.value.error_type == "purpose"
    with pytest.raises(collector.BatchRejected) as error:
        _run(db_path, tmp_path, manifest, _quotes(codes), purpose="backfill")
    assert error.value.error_type == "purpose"


def test_future_date_rejected(db_path, tmp_path):
    codes = _universe()
    manifest = _manifest(codes)
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    with pytest.raises(collector.BatchRejected) as error:
        _run(db_path, tmp_path, manifest, _quotes(codes), day=date(2026, 11, 13))
    assert error.value.error_type == "date"


def test_time_checks_use_shanghai_timezone(db_path, tmp_path):
    codes = _universe()
    manifest = _manifest(codes)
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    result, _ = _run(
        db_path, tmp_path, manifest, _quotes(codes),
        now=datetime(2026, 11, 11, 8, 0, tzinfo=ZoneInfo("UTC")),
    )
    assert result["status"] == "confirmed"

    other = tmp_path / "second"
    other.mkdir()
    db2 = other / "forward_research.db"
    _init_target(db2, other, days=CAL, now=NOW)
    with pytest.raises(collector.BatchRejected) as error:
        _run(
            db2, other, manifest, _quotes(codes),
            now=datetime(2026, 11, 11, 6, 0, tzinfo=ZoneInfo("UTC")),
        )
    assert error.value.error_type == "not_ready"


def test_corrupt_database_fails_closed(db_path, tmp_path):
    con = sqlite3.connect(db_path)
    try:
        con.execute("CREATE TABLE trade_calendar (trade_date TEXT PRIMARY KEY)")
        con.executemany("INSERT INTO trade_calendar VALUES (?)", [(item.isoformat(),) for item in CAL])
        con.commit()
    finally:
        con.close()
    codes = _universe()
    manifest = _manifest(codes)
    fetch, calls = _fetch_of(_quotes(codes))
    with pytest.raises(collector.BatchRejected) as error:
        collector.collect(
            db_path, DAY, fetch=fetch, manifest=manifest, calendar=CAL, now=NOW,
            execute=True, allowed_root=tmp_path,
        )
    assert error.value.error_type == "db_unreadable"
    assert calls == []


def test_token_is_redacted_from_errors_and_logs(db_path, tmp_path, monkeypatch):
    codes = _universe()
    manifest = _manifest(codes)
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    monkeypatch.setenv("TUSHARE_TOKEN", "secret-token-xyz")

    def bad_fetch(_day):
        raise RuntimeError("upstream rejected secret-token-xyz")

    with pytest.raises(collector.BatchRejected) as error:
        collector.collect(
            db_path, DAY, fetch=bad_fetch, manifest=manifest, calendar=CAL, now=NOW,
            execute=True, allowed_root=tmp_path,
        )
    assert "secret-token-xyz" not in str(error.value)
    stored = _rows(db_path, "SELECT message FROM ingest_log WHERE status='failed'")[0][0]
    assert "secret-token-xyz" not in stored


def test_plan_does_not_create_database(tmp_path):
    db = tmp_path / "forward_research.db"
    codes = _universe()
    manifest = _manifest(codes)
    result, calls = _run(db, tmp_path, manifest, _quotes(codes), execute=False)
    assert result["status"] == "plan"
    assert result["expected_total"] == len(codes)
    assert result["calendar_ready"] is False
    assert calls == []
    assert not db.exists()


# ---------------------------------------------------------------------------
# 正式评价审计（Tushare 数据源）
# ---------------------------------------------------------------------------


def _write_snapshot(
    out: Path,
    day: date,
    revision: int,
    codes: list[str],
    *,
    mode: str = "FORWARD_FROZEN",
    provider: str | None = "tushare",
    protocol: str = store.DATA_PROTOCOL_VERSION,
    db_name: str = "forward_research.db",
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "mode": mode, "signal_date": day.isoformat(),
            "created_at": "2026-11-11T16:10:00+08:00",
            "strategy_version": ft.STRATEGY_VERSION, "git_commit": "test",
            "source_data_revision": {"kind": "data_revision", "value": {f"stock_daily:{day}": revision}},
            "source_data_date": day.isoformat(), "universe_definition": "test",
            "universe_count": len(codes), "stock_code": code, "rank": index + 1,
            "score_total": 50.0, "rev20": 1.0, "illiq20": 1.0, "amp20": 0.02,
            "percentiles": {}, "avg_amount_20": 1e7, "eligibility_reason": "test",
        }
        for index, code in enumerate(codes)
    ]
    payload = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    stem = "signals" if mode == "FORWARD_FROZEN" else "replay_signals"
    (out / f"{stem}_{day}.jsonl").write_bytes(payload.encode("utf-8"))
    meta = {
        "mode": mode,
        "counts_for_forward_performance": mode == "FORWARD_FROZEN",
        "signal_date": day.isoformat(), "row_count": len(codes),
        "strategy_version": ft.STRATEGY_VERSION, "universe_count": len(codes),
        "completeness": {"rows": 100},
        "source_data_revision": {"kind": "data_revision", "value": {f"stock_daily:{day}": revision}},
        "content_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }
    if provider:
        meta["data_source"] = {
            "provider": provider, "db": db_name, "data_protocol": protocol,
            "revision_domain": f"stock_daily:{day}",
            "units": {
                "raw": {"vol": "手", "amount": "千元"},
                "normalized": {"volume": "股", "amount": "元"},
            },
            "pct_source": "official",
        }
    (out / f"{stem}_{day}.meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def _evaluate(db: Path, out: Path, day: date, *, mode: str = "forward") -> int:
    return ft.main([
        "--db", str(db), "--out", str(out),
        "evaluate", "--date", day.isoformat(), "--mode", mode,
    ])


def _forward_db(tmp_path: Path, *, days: int = 40):
    codes: set[str] = set()
    for prefix, size in (("600", 40), ("000", 40), ("300", 10), ("688", 5), ("920", 5)):
        codes.update(f"{prefix}{index:03d}" for index in range(size))
    manifest = _manifest(codes)
    dates: list[date] = []
    cursor = date(2026, 9, 1)
    while len(dates) < days:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor += timedelta(days=1)
    next_day = dates[-1] + timedelta(days=3)
    all_days = dates + [next_day]
    root = tmp_path / "eval"
    root.mkdir(exist_ok=True)
    db = root / "forward_research.db"
    _init_target(
        db, root, days=all_days,
        now=datetime.combine(dates[-1], datetime.min.time(), CST).replace(hour=16),
        end=next_day,
    )
    collect_now = datetime.combine(dates[-1] + timedelta(days=1), datetime.min.time(), CST).replace(hour=16)
    for item in dates:
        _run(
            db, root, manifest, _quotes(codes), day=item, now=collect_now,
            purpose="backfill", calendar=all_days,
        )
    return root, db, dates


def test_evaluate_accepts_verified_tushare_snapshot(tmp_path, capsys):
    root, db, dates = _forward_db(tmp_path)
    signal_day = dates[30]
    codes = [f"600{index:03d}" for index in range(5)]
    out = root / "forward_c0"
    _write_snapshot(out, signal_day, 1, codes)
    jsonl = out / f"signals_{signal_day}.jsonl"
    before = hashlib.sha256(jsonl.read_bytes()).hexdigest()
    assert _evaluate(db, out, signal_day) == 0
    assert hashlib.sha256(jsonl.read_bytes()).hexdigest() == before
    meta = json.loads((out / f"signals_{signal_day}.meta.json").read_text(encoding="utf-8"))
    current = {
        "kind": "data_revision",
        "value": {"stock_daily": 40, f"stock_daily:{signal_day}": 1},
    }
    assert ft.revision_mismatch(meta, current, signal_day) is None
    capsys.readouterr()


def test_protocol_legacy_text_does_not_block_forward_audit(tmp_path, capsys, monkeypatch):
    root, db, dates = _forward_db(tmp_path)
    signal_day = dates[30]
    out = root / "forward_c0"
    _write_snapshot(out, signal_day, 1, [f"600{index:03d}" for index in range(5)])
    # 旧说明文本写的是 history.db；评价不应读取这段描述
    monkeypatch.setitem(ft.PROTOCOL, "data_source", "backend/data/history.db（SQLite mode=ro）")
    assert _evaluate(db, out, signal_day) == 0
    capsys.readouterr()


def test_audit_covers_d_plus_19(tmp_path, capsys):
    root, db, dates = _forward_db(tmp_path)
    signal_day = dates[20]
    out = root / "forward_c0"
    _write_snapshot(out, signal_day, 1, [f"600{index:03d}" for index in range(5)])
    assert ft.AUDIT_TRADING_DAYS == 19
    assert _evaluate(db, out, signal_day) == 0
    capsys.readouterr()


@pytest.mark.parametrize("offset", [11, 19])
def test_audit_rejects_tampered_d_plus_11_or_19(tmp_path, capsys, offset):
    root, db, dates = _forward_db(tmp_path)
    signal_day = dates[20]
    out = root / "forward_c0"
    _write_snapshot(out, signal_day, 1, [f"600{index:03d}" for index in range(5)])
    target_day = dates[20 + offset]
    con = sqlite3.connect(db)
    try:
        con.execute(
            "UPDATE stock_daily SET close=close+1 WHERE trade_date=? AND code='600000'",
            (target_day.isoformat(),),
        )
        con.commit()
    finally:
        con.close()
    with pytest.raises(SystemExit) as error:
        _evaluate(db, out, signal_day)
    assert target_day.isoformat() in str(error.value)
    capsys.readouterr()


def test_audit_rejects_d_plus_19_marker_without_rows(tmp_path, capsys):
    root, db, dates = _forward_db(tmp_path)
    signal_day, target_day = dates[20], dates[39]
    out = root / "forward_c0"
    _write_snapshot(out, signal_day, 1, [f"600{index:03d}" for index in range(5)])
    con = sqlite3.connect(db)
    try:
        con.execute("DELETE FROM stock_daily WHERE trade_date=?", (target_day.isoformat(),))
        con.commit()
    finally:
        con.close()
    with pytest.raises(SystemExit) as error:
        _evaluate(db, out, signal_day)
    assert "完成标记" in str(error.value)
    capsys.readouterr()


def test_audit_rejects_d_plus_19_rows_without_success_log(tmp_path, capsys):
    root, db, dates = _forward_db(tmp_path)
    signal_day, target_day = dates[20], dates[39]
    out = root / "forward_c0"
    _write_snapshot(out, signal_day, 1, [f"600{index:03d}" for index in range(5)])
    con = sqlite3.connect(db)
    try:
        con.execute(
            "DELETE FROM ingest_log WHERE trade_date=? AND status='ok'", (target_day.isoformat(),)
        )
        con.commit()
    finally:
        con.close()
    with pytest.raises(SystemExit) as error:
        _evaluate(db, out, signal_day)
    assert "成功采集日志" in str(error.value)
    capsys.readouterr()


def test_audit_rejects_gap_inside_dependency_window(tmp_path, capsys):
    root, db, dates = _forward_db(tmp_path)
    signal_day, gap_day = dates[20], dates[25]
    out = root / "forward_c0"
    _write_snapshot(out, signal_day, 1, [f"600{index:03d}" for index in range(5)])
    con = sqlite3.connect(db)
    try:
        con.execute("DELETE FROM stock_daily WHERE trade_date=?", (gap_day.isoformat(),))
        con.execute("DELETE FROM universe_status WHERE trade_date=?", (gap_day.isoformat(),))
        con.execute("DELETE FROM ingest_log WHERE trade_date=?", (gap_day.isoformat(),))
        con.execute("DELETE FROM data_revision WHERE domain=?", (f"stock_daily:{gap_day}",))
        con.commit()
    finally:
        con.close()
    with pytest.raises(SystemExit) as error:
        _evaluate(db, out, signal_day)
    assert "缺口" in str(error.value)
    capsys.readouterr()


def test_future_days_remain_pending_without_fake_returns(tmp_path, capsys):
    root, db, dates = _forward_db(tmp_path)
    signal_day = dates[-1]
    out = root / "forward_c0"
    _write_snapshot(out, signal_day, 1, [f"600{index:03d}" for index in range(5)])
    assert _evaluate(db, out, signal_day) == 0
    text = capsys.readouterr().out
    assert "pending" in text
    assert "不计入任何收益" in text


def test_snapshot_files_untouched_when_later_days_are_appended(tmp_path, capsys):
    root, db, dates = _forward_db(tmp_path)
    signal_day = dates[30]
    codes = set()
    for prefix, size in (("600", 40), ("000", 40), ("300", 10), ("688", 5), ("920", 5)):
        codes.update(f"{prefix}{index:03d}" for index in range(size))
    manifest = _manifest(codes)
    out = root / "forward_c0"
    _write_snapshot(out, signal_day, 1, [f"600{index:03d}" for index in range(5)])
    jsonl = out / f"signals_{signal_day}.jsonl"
    before = hashlib.sha256(jsonl.read_bytes()).hexdigest()

    next_day = dates[-1] + timedelta(days=3)
    all_days = dates + [next_day]
    collect_now = datetime.combine(next_day + timedelta(days=1), datetime.min.time(), CST).replace(hour=16)
    result, _ = _run(
        db, root, manifest, _quotes(codes), day=next_day, now=collect_now,
        purpose="backfill", calendar=all_days,
    )
    assert result["status"] == "confirmed"
    assert hashlib.sha256(jsonl.read_bytes()).hexdigest() == before
    state = ft.inspect_snapshot(out, signal_day, ft.MODE_FORWARD)
    assert state.state == ft.STATE_COMPLETE
    assert _evaluate(db, out, signal_day) == 0
    capsys.readouterr()


@pytest.mark.parametrize("tamper", ["delete_log", "drop_table", "corrupt_digest", "signal_rows", "entry_rows", "exit_rows"])
def test_evaluate_rejects_audit_failures(tmp_path, capsys, tamper):
    root, db, dates = _forward_db(tmp_path)
    signal_day = dates[30]
    entry_day = dates[31]
    exit_day = dates[35]
    out = root / "forward_c0"
    _write_snapshot(out, signal_day, 1, [f"600{index:03d}" for index in range(5)])

    con = sqlite3.connect(db)
    try:
        if tamper == "delete_log":
            con.execute(
                "DELETE FROM ingest_log WHERE trade_date=? AND status='ok'", (signal_day.isoformat(),)
            )
        elif tamper == "drop_table":
            con.execute("DROP TABLE ingest_log")
        elif tamper == "corrupt_digest":
            message = con.execute(
                "SELECT message FROM ingest_log WHERE trade_date=? AND status='ok'", (signal_day.isoformat(),)
            ).fetchone()[0]
            con.execute(
                "UPDATE ingest_log SET message=? WHERE trade_date=? AND status='ok'",
                (message.replace("tushare-daily:", "tampered-daily:"), signal_day.isoformat()),
            )
        elif tamper == "signal_rows":
            con.execute(
                "UPDATE stock_daily SET close=close+1 WHERE trade_date=? AND code='600000'",
                (signal_day.isoformat(),),
            )
        elif tamper == "entry_rows":
            con.execute(
                "UPDATE stock_daily SET close=close+1 WHERE trade_date=? AND code='600000'",
                (entry_day.isoformat(),),
            )
        elif tamper == "exit_rows":
            con.execute(
                "UPDATE stock_daily SET close=close+1 WHERE trade_date=? AND code='600000'",
                (exit_day.isoformat(),),
            )
        con.commit()
    finally:
        con.close()

    with pytest.raises(SystemExit):
        _evaluate(db, out, signal_day)
    capsys.readouterr()


def test_evaluate_fails_closed_when_marker_has_no_rows(tmp_path, capsys):
    root, db, dates = _forward_db(tmp_path)
    signal_day, entry_day = dates[30], dates[31]
    out = root / "forward_c0"
    _write_snapshot(out, signal_day, 1, [f"600{index:03d}" for index in range(5)])

    con = sqlite3.connect(db)
    try:
        con.execute("DELETE FROM stock_daily WHERE trade_date=?", (entry_day.isoformat(),))
        con.commit()
    finally:
        con.close()

    with pytest.raises(SystemExit) as error:
        _evaluate(db, out, signal_day)
    assert "完成标记" in str(error.value)
    capsys.readouterr()


@pytest.mark.parametrize("provider", ["history", "unknown", None])
def test_evaluate_rejects_non_tushare_forward_source(tmp_path, capsys, provider):
    root, db, dates = _forward_db(tmp_path)
    signal_day = dates[30]
    out = root / "forward_c0"
    _write_snapshot(
        out, signal_day, 1, [f"600{index:03d}" for index in range(5)], provider=provider
    )
    with pytest.raises(SystemExit) as error:
        _evaluate(db, out, signal_day)
    assert "不是 Tushare 前瞻协议" in str(error.value)
    capsys.readouterr()


def test_replay_is_not_forced_into_forward_audit(tmp_path, capsys):
    root, db, dates = _forward_db(tmp_path)
    day = dates[30]
    out = root / "forward_c0"
    _write_snapshot(out, day, 1, [f"600{index:03d}" for index in range(5)], mode="REPLAY_ONLY", provider=None)
    assert _evaluate(db, out, day, mode="replay") == 0
    text = capsys.readouterr().out
    assert "REPLAY_ONLY" in text


def test_snapshot_meta_records_tushare_source(tmp_path):
    root, db, dates = _forward_db(tmp_path)
    manifest_path, _sha = _raw_manifest(root, _universe(), version="v-test")
    out = root / "forward_c0"
    last = dates[-1]
    code = ft.main([
        "--db", str(db), "--out", str(out), "--universe", str(manifest_path),
        "snapshot", "--mode", "forward", "--date", last.isoformat(),
        "--now", datetime.combine(last, datetime.min.time(), CST).replace(hour=15, minute=45).isoformat(),
    ])
    assert code == 0
    meta = json.loads((out / f"signals_{last}.meta.json").read_text(encoding="utf-8"))
    source = meta["data_source"]
    assert source["provider"] == "tushare"
    assert source["db"] == "forward_research.db"
    assert source["data_protocol"] == store.DATA_PROTOCOL_VERSION
    assert source["units"]["raw"] == {"vol": "手", "amount": "千元"}
    assert source["units"]["normalized"] == {"volume": "股", "amount": "元"}
    assert meta["universe"]["version"] == "v-test"
    assert meta["mode"] == "FORWARD_FROZEN"


# ---------------------------------------------------------------------------
# 采集器 schema（universe_status）
# ---------------------------------------------------------------------------


def test_store_schema_has_universe_status(tmp_path):
    engine = store.connect(tmp_path / "forward_research.db")
    store.create_schema(engine)
    with engine.begin() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(universe_status)")}
    engine.dispose()
    assert columns == {"trade_date", "code", "status", "reason"}
