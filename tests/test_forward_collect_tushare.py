"""Tushare 前瞻采集器的离线测试（合成数据，不联网、不碰真实库）。

覆盖：首次采集的冻结清单基线、分市场整段缺失、停牌与未证实缺失的区分、
单事务四件套（行情/逐券状态/完成标记/成功日志）的故障注入、修订与快照冻结关联、
目标库路径别名/链接保护、日历初始化与 40 日预检、时区与损坏库失败关闭。
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

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "backend", ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts import forward_collect_tushare as collector  # noqa: E402
from scripts import forward_store as store  # noqa: E402

CST = ZoneInfo("Asia/Shanghai")
DAY = date(2026, 11, 11)
PREV = date(2026, 11, 10)
NEXT = date(2026, 11, 12)
NOW = datetime(2026, 11, 11, 16, 0, tzinfo=CST)
CAL = [PREV, DAY, NEXT]


# ---------------------------------------------------------------------------
# 合成数据
# ---------------------------------------------------------------------------


def _universe() -> set[str]:
    codes: list[str] = []
    for prefix, size in (("600", 40), ("688", 20), ("000", 60), ("300", 40), ("920", 20)):
        codes.extend(f"{prefix}{index:03d}" for index in range(size))
    return set(codes)


def _big_universe() -> set[str]:
    codes: list[str] = []
    for prefix, size in (("600", 400), ("000", 400), ("300", 140), ("688", 40), ("920", 20)):
        codes.extend(f"{prefix}{index:03d}" for index in range(size))
    return set(codes)


def _manifest(codes: set[str] | None = None, *, version: str = "test-v1") -> collector.Manifest:
    chosen = set(codes or _universe())
    counts: dict[str, int] = {}
    for code in chosen:
        board = store.board_of(code)
        counts[board] = counts.get(board, 0) + 1
    return collector.Manifest(
        version=version,
        eligibility_sha256="test-eligibility",
        universe_sha256="test-universe",
        data_cutoff="2026-10-19",
        codes=frozenset(chosen),
        board_counts=counts,
        total=len(chosen),
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
        snapshot_dir=tmp_path / "forward_c0",
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


def _raw_manifest(tmp_path: Path, codes: set[str], *, version: str = "v-test") -> tuple[Path, str]:
    """按 `forward_universe.build_manifest` 的哈希口径写一份合成清单。"""
    ordered = [
        {
            "code": code,
            "name": f"测试{code}",
            "board": store.board_of(code),
            "forward_eligible": True,
            "identity_verified": True,
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
# 清单校验
# ---------------------------------------------------------------------------


def test_real_frozen_manifest_is_eligible_only(tmp_path):
    manifest = collector.load_manifest(collector.DEFAULT_MANIFEST)
    assert manifest.version == "b1v-2026-11-11"
    assert manifest.total == 5108
    assert manifest.board_counts["北交所"] == 32
    assert len(manifest.codes) == 5108


def test_manifest_version_mismatch_rejected(tmp_path):
    path, sha = _raw_manifest(tmp_path, _universe())
    with pytest.raises(SystemExit):
        collector.load_manifest(path, expect_version="v-other", expect_eligibility_sha256=sha)


def test_manifest_eligibility_hash_mismatch_rejected(tmp_path):
    path, sha = _raw_manifest(tmp_path, _universe())
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["securities"][0]["forward_eligible"] = False  # 手工改资格但不改哈希
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit):
        collector.load_manifest(path, expect_version="v-test", expect_eligibility_sha256=sha)


def test_manifest_rejects_unverified_eligible(tmp_path):
    path, sha = _raw_manifest(tmp_path, _universe())
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["securities"][0]["identity_verified"] = False
    # 同步资格哈希，确保失败原因是「身份未核验」而不是哈希
    eligibility = "\n".join(
        f"{item['code']}|{int(bool(item['forward_eligible']))}"
        for item in sorted(payload["securities"], key=lambda item: item["code"])
    )
    payload["eligibility_sha256"] = hashlib.sha256(eligibility.encode("utf-8")).hexdigest()
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit):
        collector.load_manifest(
            path, expect_version="v-test", expect_eligibility_sha256=payload["eligibility_sha256"]
        )


# ---------------------------------------------------------------------------
# 正常日 / 幂等 / 清单外证券
# ---------------------------------------------------------------------------


def test_normal_day_confirms_units_and_atomic_pieces(db_path, tmp_path):
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
    log = _rows(db_path, "SELECT message FROM ingest_log WHERE status='ok'")[0][0]
    message = json.loads(log)
    assert message["revision"] == 1 and message["purpose"] == "live"
    assert message["manifest_version"] == "test-v1"


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


def test_extra_codes_are_stored_but_not_counted_in_coverage(db_path, tmp_path):
    codes = _universe()
    manifest = _manifest(codes)
    payload = _quotes(codes) + _quotes({"601999"})
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    result, _ = _run(db_path, tmp_path, manifest, payload)
    assert result["status"] == "confirmed"
    assert _count(db_path, "SELECT COUNT(*) FROM stock_daily WHERE code='601999'") == 1
    message = json.loads(_rows(db_path, "SELECT message FROM ingest_log WHERE status='ok'")[0][0])
    assert message["extras"] == 1
    assert message["by_board_eligible"].get("沪主板") == 40


# ---------------------------------------------------------------------------
# 首次采集的分市场基线
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
    assert error.value.error_type == "incomplete"
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


def test_first_collect_allows_partial_normal_suspension(db_path, tmp_path):
    codes = _big_universe()
    manifest = _manifest(codes)
    payload = _quotes([code for code in codes if code != "920000"])
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    result, _ = _run(db_path, tmp_path, manifest, payload, min_cov=collector.MIN_UNIVERSE_COVERAGE_FLOOR)
    assert result["status"] == "confirmed"
    status = _rows(db_path, "SELECT status, reason FROM universe_status WHERE code='920000'")[0]
    assert status == ("no_quote_unverified", "absent_from_daily")


def test_previous_confirmed_day_is_the_board_baseline(db_path, tmp_path):
    codes = _big_universe()
    manifest = _manifest(codes)
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    _run(db_path, tmp_path, manifest, _quotes(codes), day=PREV, purpose="backfill")
    dropped = [f"000{index:03d}" for index in range(30)]
    payload = _quotes(codes - set(dropped))
    with pytest.raises(collector.BatchRejected) as error:
        _run(
            db_path, tmp_path, manifest, payload,
            day=DAY, purpose="live", min_cov=collector.MIN_UNIVERSE_COVERAGE_FLOOR,
        )
    assert any("上一确认日" in item and "深主板" in item for item in error.value.problems)
    assert _count(db_path, "SELECT COUNT(*) FROM stock_daily WHERE trade_date=?", (DAY.isoformat(),)) == 0
    assert _count(db_path, "SELECT COUNT(*) FROM stock_daily WHERE trade_date=?", (PREV.isoformat(),)) == len(codes)


def test_suspended_evidence_upgrades_status_only_for_missing(db_path, tmp_path):
    codes = _universe()
    manifest = _manifest(codes)
    payload = _quotes([code for code in codes if code != "600000"])
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    _run(db_path, tmp_path, manifest, payload, suspended={"600000": "交易所停牌公告 2026-11-11"})
    status = _rows(db_path, "SELECT status, reason FROM universe_status WHERE code='600000'")[0]
    assert status == ("suspended_confirmed", "交易所停牌公告 2026-11-11")

    with pytest.raises(collector.BatchRejected) as error:
        _run(
            db_path, tmp_path, manifest, _quotes(codes),
            day=PREV, purpose="backfill", suspended={"600000": "冲突证据"},
        )
    assert error.value.error_type == "suspended_conflict"


# ---------------------------------------------------------------------------
# 单事务：行情 / 状态 / 完成标记 / 成功日志
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
# 修订与冻结快照关联
# ---------------------------------------------------------------------------


def _write_frozen(tmp_path: Path, day: date, *, revision: int) -> None:
    from scripts import forward_track_c0 as ft

    out = tmp_path / "forward_c0"
    out.mkdir(parents=True, exist_ok=True)
    rows = [{
        "mode": "FORWARD_FROZEN", "signal_date": day.isoformat(),
        "created_at": "2026-11-11T16:10:00+08:00",
        "strategy_version": ft.STRATEGY_VERSION, "git_commit": "test",
        "source_data_revision": {"kind": "data_revision", "value": {f"stock_daily:{day}": revision}},
        "source_data_date": day.isoformat(), "universe_definition": "test",
        "universe_count": 1, "stock_code": "600000", "rank": 1, "score_total": 50.0,
        "rev20": 1.0, "illiq20": 1.0, "amp20": 0.02, "percentiles": {},
        "avg_amount_20": 1e7, "eligibility_reason": "test",
    }]
    payload = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    (out / f"signals_{day}.jsonl").write_bytes(payload.encode("utf-8"))
    meta = {
        "mode": "FORWARD_FROZEN", "counts_for_forward_performance": True,
        "signal_date": day.isoformat(), "row_count": 1,
        "strategy_version": ft.STRATEGY_VERSION, "universe_count": 1,
        "completeness": {"rows": 5000},
        "source_data_revision": {"kind": "data_revision", "value": {f"stock_daily:{day}": revision}},
        "content_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }
    (out / f"signals_{day}.meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def test_revise_allowed_before_any_frozen_snapshot(db_path, tmp_path):
    codes = _universe()
    manifest = _manifest(codes)
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    _run(db_path, tmp_path, manifest, _quotes(codes))
    result, _ = _run(
        db_path, tmp_path, manifest, _quotes(codes, close=11.0),
        revise=True, reason="源数据晚到修订",
    )
    assert result["revision"] == 2
    assert result["data_version"].endswith(":r2")
    assert _rows(db_path, "SELECT DISTINCT close FROM stock_daily") == [(11.0,)]
    message = json.loads(_rows(db_path, "SELECT message FROM ingest_log WHERE status='ok' ORDER BY id DESC LIMIT 1")[0][0])
    assert message["revised"] is True
    assert message["revise_reason"] == "源数据晚到修订"


def test_revise_rejected_after_forward_frozen_snapshot(db_path, tmp_path):
    codes = _universe()
    manifest = _manifest(codes)
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    _run(db_path, tmp_path, manifest, _quotes(codes))
    _write_frozen(tmp_path, DAY, revision=1)
    with pytest.raises(collector.BatchRejected) as error:
        _run(db_path, tmp_path, manifest, _quotes(codes, close=11.0), revise=True, reason="晚到修订")
    assert error.value.error_type == "frozen"
    assert _rows(db_path, "SELECT DISTINCT close FROM stock_daily") == [(10.0,)]


def test_revise_requires_reason_and_existing_revision(db_path, tmp_path):
    codes = _universe()
    manifest = _manifest(codes)
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    with pytest.raises(collector.BatchRejected):
        _run(db_path, tmp_path, manifest, _quotes(codes), revise=True, reason="x")
    _run(db_path, tmp_path, manifest, _quotes(codes))
    with pytest.raises(collector.BatchRejected) as error:
        _run(db_path, tmp_path, manifest, _quotes(codes, close=11.0), revise=True, reason="")
    assert error.value.error_type == "config"


def test_failed_revise_keeps_original_version(db_path, tmp_path, monkeypatch):
    codes = _universe()
    manifest = _manifest(codes)
    _init_target(db_path, tmp_path, days=CAL, now=NOW)
    _run(db_path, tmp_path, manifest, _quotes(codes))
    before = _rows(db_path, "SELECT close, data_version FROM stock_daily WHERE code='600000'")

    real = collector._write_confirmed

    def boom(engine, batch, **kwargs):
        return real(engine, batch, _failpoint="status", **kwargs)

    monkeypatch.setattr(collector, "_write_confirmed", boom)
    with pytest.raises(RuntimeError):
        _run(db_path, tmp_path, manifest, _quotes(codes, close=11.0), revise=True, reason="晚到修订")
    assert _rows(db_path, "SELECT close, data_version FROM stock_daily WHERE code='600000'") == before
    assert _count(db_path, "SELECT revision FROM data_revision WHERE domain=?", (f"stock_daily:{DAY}",)) == 1


def test_evaluate_rejects_changed_data_revision():
    from scripts import forward_track_c0 as ft

    meta = {"source_data_revision": {"kind": "data_revision", "value": {f"stock_daily:{DAY}": 1}}}
    current = {"kind": "data_revision", "value": {f"stock_daily:{DAY}": 2}}
    assert ft.revision_mismatch(meta, current, DAY)
    assert ft.revision_mismatch(meta, {"kind": "data_revision", "value": {f"stock_daily:{DAY}": 1}}, DAY) is None
    # 删掉版本表/当日完成标记 → 视为绕过，拒绝
    assert ft.revision_mismatch(meta, {"kind": "file_stat", "value": {}}, DAY)
    assert ft.revision_mismatch(meta, {"kind": "data_revision", "value": {"stock_daily": 9}}, DAY)
    # 历史库（快照本身没记录 data_revision）保持原行为
    legacy_meta = {"source_data_revision": {"kind": "file_stat", "value": {}}}
    assert ft.revision_mismatch(legacy_meta, {"kind": "data_revision", "value": {}}, DAY) is None
    # 快照缺记录但库内有当日版本 → 拒绝
    assert ft.revision_mismatch({}, {"kind": "data_revision", "value": {f"stock_daily:{DAY}": 1}}, DAY)


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
    # 源库不变
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
            execute=True, snapshot_dir=tmp_path / "forward_c0", allowed_root=tmp_path,
        )
    assert error.value.error_type == "calendar"
    assert calls == []


def test_forty_days_plus_calendar_passes_forward_preflight(db_path, tmp_path):
    from sqlalchemy.orm import Session

    from scripts import forward_track_c0 as ft

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

    _init_target(db_path, tmp_path, days=all_days, now=datetime.combine(last, datetime.min.time(), CST).replace(hour=16), end=next_day)
    collect_now = datetime.combine(last + timedelta(days=1), datetime.min.time(), CST).replace(hour=16)
    for item in days:
        _run(
            db_path, tmp_path, manifest, _quotes(codes),
            day=item, now=collect_now,
            purpose="backfill", calendar=all_days,
        )

    engine = store.connect_readonly(db_path)
    try:
        with Session(engine) as session:
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
            execute=True, min_universe_coverage=0.0,
            snapshot_dir=tmp_path / "forward_c0", allowed_root=tmp_path,
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
    # UTC 08:00 = 上海 16:00，允许
    result, _ = _run(
        db_path, tmp_path, manifest, _quotes(codes),
        now=datetime(2026, 11, 11, 8, 0, tzinfo=ZoneInfo("UTC")),
    )
    assert result["status"] == "confirmed"

    # 另一份库：UTC 06:00 = 上海 14:00，不允许
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
            execute=True, snapshot_dir=tmp_path / "forward_c0", allowed_root=tmp_path,
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
            execute=True, snapshot_dir=tmp_path / "forward_c0", allowed_root=tmp_path,
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
    assert result["calendar_ready"] is False
    assert calls == []
    assert not db.exists()
