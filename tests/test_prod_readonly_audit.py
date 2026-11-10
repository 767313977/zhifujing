"""P1.3-S1 隔离机只读审计工具的安全负面测试。

被测对象：`backend/data/p13_audit/prod_readonly_audit.py`

三条纪律：

1. **不降低安全要求**：本文件只验证「该拒绝的必须拒绝」，不提供任何绕过校验的用例；
2. **标注真实执行环境**：`PLATFORM` / `POSIX_ONLY` 决定用例实际执行还是 SKIP，
   报告（`snapshot_security_test_report.md`）逐条记录真实环境与结果；
3. **只用代理副本**：所有会走完整流程的用例都在 `tmp_path` 里的副本上跑，
   不接触真实生产快照，也不声称任何真实生产结论。

用例分组：
- A 路径与位置（file URI / UNC / 生产路径分量 / 相对路径 / 越界 / 软链 / 硬链 / 目录）
- B 快照静态性（伴随文件 / WAL 头 / 非 SQLite）
- C POSIX 权限（root / 属主 / 权限过宽）——仅 POSIX 执行
- D 摘要与大小（SHA 不符 / 大小不符 / snapshot 未声明大小 / 运行中被改）
- E SQLite 只读模型（query_only / authorizer 拒 DML·DDL·ATTACH / 语句白名单 / PRAGMA 白名单）
- F 输出最小化（顶层与深层白名单 / 敏感值 / 超长 / 超量）
- G 资源守卫（超时 / RSS 超限）
- H 命令行（无 --force / --ignore-safety 等跳过开关）
- I 正向冒烟（replica=PASS 且 real_prod_eligible=false；snapshot 在非 POSIX 必不 PASS）
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
AUDIT_DIR = REPO / "backend" / "data" / "p13_audit"
REPLICA = AUDIT_DIR / "prod_replica.db"

sys.path.insert(0, str(AUDIT_DIR))

import prod_readonly_audit as audit  # noqa: E402

PLATFORM = f"{os.name}/{sys.platform}"
POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason=f"POSIX 专属检查（属主/权限/root），当前环境 {PLATFORM} 不执行"
)


# --------------------------------------------------------------------- 夹具


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    """授权根 + 干净副本（单链接、无伴随文件、legacy journal 模式）。"""
    if not REPLICA.exists():
        pytest.skip("缺少代理副本 prod_replica.db")
    allow = tmp_path / "p13"
    allow.mkdir()
    shutil.copyfile(REPLICA, allow / "fupan-snapshot.db")
    return allow.resolve()


def _db(allow: Path) -> Path:
    return allow / "fupan-snapshot.db"


def _argv(allow: Path, db: Path, out: Path, **over) -> list[str]:
    argv = [
        "--source-kind",
        over.get("kind", "replica"),
        "--db",
        str(over.get("db_arg", db)),
        "--allow-dir",
        str(over.get("allow_arg", allow)),
        "--out",
        str(out),
        "--expect-sha256",
        over.get("sha", audit.sha256_file(db)),
    ]
    if "size" in over:
        argv += ["--expect-size", str(over["size"])]
    else:
        argv += ["--expect-size", str(db.stat().st_size)]
    for name in ("fingerprint", "schema_ref", "compare_db"):
        if name in over:
            argv += ["--" + name.replace("_", "-"), str(over[name])]
    if over.get("no_date_series"):
        argv += ["--no-date-series"]
    if over.get("timeout_s") is not None:
        argv += ["--timeout-s", str(over["timeout_s"])]
    if over.get("max_rss_mb") is not None:
        argv += ["--max-rss-mb", str(over["max_rss_mb"])]
    return argv


def _run(allow: Path, db: Path, out: Path, **over) -> tuple[int, dict | None]:
    code = audit.run(audit.build_parser().parse_args(_argv(allow, db, out, **over)))
    evidence = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return code, evidence


# --------------------------------------------------------------------- A 路径


def test_a_file_uri_rejected(workdir: Path) -> None:
    allow = workdir
    with pytest.raises(audit.AuditRefusal) as info:
        audit.resolve_input(f"file:{_db(allow).as_posix()}?mode=ro", allow)
    assert info.value.code == "REFUSE_FILE_URI"


def test_a_unc_path_rejected(workdir: Path) -> None:
    with pytest.raises(audit.AuditRefusal) as info:
        audit.resolve_input("\\\\server\\share\\fupan-snapshot.db", workdir)
    assert info.value.code == "REFUSE_UNC_PATH"


@pytest.mark.parametrize(
    "bad",
    [
        "/opt/fupan-backups/p13_mlhZicVV/fupan-snapshot.db",
        "/opt/fupan/data.db",
        "/var/lib/fupan/fupan.db",
    ],
)
def test_a_production_path_rejected(workdir: Path, bad: str) -> None:
    with pytest.raises(audit.AuditRefusal) as info:
        audit.resolve_input(bad, workdir)
    assert info.value.code == "REFUSE_PRODUCTION_PATH"


def test_a_production_component_not_string_prefix() -> None:
    """按路径分量比较：/opt/fupan2 不是生产目录，/opt/fupan/x 是。"""
    assert audit.is_production_path(Path("/opt/fupan/x/fupan.db")) is True
    assert audit.is_production_path(Path("/opt/fupan-backups/x/fupan.db")) is True
    assert audit.is_production_path(Path("/opt/fupan2/x/fupan.db")) is False
    assert audit.is_production_path(Path("/srv/p13/in/fupan-snapshot.db")) is False


def test_a_relative_path_resolved_then_checked(workdir: Path) -> None:
    """相对路径先解析；解析后仍在授权根内即合法，且事实里记录 was_relative。"""
    cwd = Path.cwd()
    try:
        os.chdir(workdir)
        _, resolved, facts = audit.resolve_input("fupan-snapshot.db", workdir)
    finally:
        os.chdir(cwd)
    assert facts["was_relative"] is True
    assert resolved == _db(workdir).resolve()


def test_a_dotdot_escape_rejected(workdir: Path) -> None:
    outside = workdir.parent / "outside.db"
    shutil.copyfile(_db(workdir), outside)
    with pytest.raises(audit.AuditRefusal) as info:
        audit.resolve_input(str(workdir / ".." / "outside.db"), workdir)
    assert info.value.code == "REFUSE_OUTSIDE_ALLOW_DIR"


def test_a_missing_input_rejected(workdir: Path) -> None:
    with pytest.raises(audit.AuditRefusal) as info:
        audit.resolve_input(str(workdir / "nope.db"), workdir)
    assert info.value.code == "REFUSE_INPUT_MISSING"


def test_a_directory_input_rejected(workdir: Path) -> None:
    with pytest.raises(audit.AuditRefusal) as info:
        audit.resolve_input(str(workdir), workdir)
    assert info.value.code == "REFUSE_INPUT_IS_DIR"


def test_a_symlink_input_rejected(workdir: Path) -> None:
    link = workdir / "link.db"
    try:
        os.symlink(_db(workdir), link)
    except (OSError, NotImplementedError):
        pytest.skip("当前 Windows 账号无创建符号链接权限")
    with pytest.raises(audit.AuditRefusal) as info:
        audit.resolve_input(str(link), workdir)
    assert info.value.code == "REFUSE_SYMLINK_INPUT"


def test_a_hardlink_input_rejected(workdir: Path) -> None:
    link = workdir / "hard.db"
    try:
        os.link(_db(workdir), link)
    except OSError:
        pytest.skip("当前文件系统不支持硬链接")
    with pytest.raises(audit.AuditRefusal) as info:
        audit.resolve_input(str(link), workdir)
    assert info.value.code == "REFUSE_HARDLINK_INPUT"


def test_a_output_in_production_rejected(workdir: Path) -> None:
    with pytest.raises(audit.AuditRefusal) as info:
        audit.resolve_output("/opt/fupan-backups/out.json", workdir, _db(workdir))
    assert info.value.code == "REFUSE_OUT_PRODUCTION"


def test_a_output_outside_allow_dir_rejected(workdir: Path) -> None:
    outside = workdir.parent / "out.json"
    with pytest.raises(audit.AuditRefusal) as info:
        audit.resolve_output(str(outside), workdir, _db(workdir))
    assert info.value.code == "REFUSE_OUT_OUTSIDE_ALLOW_DIR"


def test_a_output_symlink_rejected(workdir: Path) -> None:
    target = workdir / "real.json"
    target.write_text("{}", encoding="utf-8")
    link = workdir / "out.json"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("当前 Windows 账号无创建符号链接权限")
    with pytest.raises(audit.AuditRefusal) as info:
        audit.resolve_output(str(link), workdir, _db(workdir))
    assert info.value.code == "REFUSE_OUT_SYMLINK"


def test_a_output_cannot_overwrite_input(workdir: Path) -> None:
    with pytest.raises(audit.AuditRefusal) as info:
        audit.resolve_output(str(_db(workdir)), workdir, _db(workdir))
    assert info.value.code == "REFUSE_OUT_IS_INPUT"


def test_a_allow_dir_missing_rejected() -> None:
    with pytest.raises(audit.AuditRefusal) as info:
        audit.resolve_allow_dir("")
    assert info.value.code == "REFUSE_ALLOW_DIR_MISSING"


# --------------------------------------------------------------------- B 静态性


def test_b_sidecar_presence_rejected(workdir: Path) -> None:
    Path(str(_db(workdir)) + "-wal").write_bytes(b"")
    with pytest.raises(audit.AuditRefusal) as info:
        audit.resolve_input(str(_db(workdir)), workdir)
    assert info.value.code == "REFUSE_SIDECAR_PRESENT"


def test_b_wal_header_rejected(workdir: Path) -> None:
    path = _db(workdir)
    blob = bytearray(path.read_bytes())
    blob[18] = 2
    blob[19] = 2
    path.write_bytes(bytes(blob))
    with pytest.raises(audit.AuditRefusal) as info:
        audit.resolve_input(str(path), workdir)
    assert info.value.code == "REFUSE_WAL_MODE_DB"


def test_b_non_sqlite_rejected(workdir: Path) -> None:
    path = workdir / "fake.db"
    path.write_bytes(b"not a database" * 20)
    with pytest.raises(audit.AuditRefusal) as info:
        audit.resolve_input(str(path), workdir)
    assert info.value.code == "REFUSE_NOT_SQLITE"


# --------------------------------------------------------------------- C POSIX


@POSIX_ONLY
def test_c_root_rejected(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit.os, "geteuid", lambda: 0)
    with pytest.raises(audit.AuditRefusal) as info:
        audit.resolve_input(str(_db(workdir)), workdir)
    assert info.value.code == "REFUSE_RUNNING_AS_ROOT"


@POSIX_ONLY
def test_c_owner_mismatch_rejected(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit.os, "geteuid", lambda: 424242)
    with pytest.raises(audit.AuditRefusal) as info:
        audit.resolve_input(str(_db(workdir)), workdir)
    assert info.value.code == "REFUSE_OWNER_MISMATCH"


@POSIX_ONLY
def test_c_permission_too_open_rejected(workdir: Path) -> None:
    path = _db(workdir)
    path.chmod(0o666)
    with pytest.raises(audit.AuditRefusal) as info:
        audit.resolve_input(str(path), workdir)
    assert info.value.code == "REFUSE_PERMISSION_TOO_OPEN"


# --------------------------------------------------------------------- D 摘要


def test_d_sha256_mismatch_stops(workdir: Path) -> None:
    allow = workdir
    code, evidence = _run(
        allow, _db(allow), allow / "out.json", sha="0" * 64
    )
    assert code == 2
    assert evidence["status"] == "FAIL_SHA256_MISMATCH"
    assert evidence["checks"] == {}


def test_d_size_mismatch_stops(workdir: Path) -> None:
    allow = workdir
    code, evidence = _run(allow, _db(allow), allow / "out.json", size=1)
    assert code == 2
    assert evidence["status"] == "FAIL_SIZE_MISMATCH"


def test_d_snapshot_requires_declared_size(workdir: Path) -> None:
    allow = workdir
    argv = [
        "--source-kind",
        "snapshot",
        "--db",
        str(_db(allow)),
        "--allow-dir",
        str(allow),
        "--out",
        str(allow / "out.json"),
        "--expect-sha256",
        audit.sha256_file(_db(allow)),
    ]
    code = audit.run(audit.build_parser().parse_args(argv))
    evidence = json.loads((allow / "out.json").read_text(encoding="utf-8"))
    assert code == 2
    assert evidence["status"] == "REFUSE_SIZE_UNDECLARED"


def test_d_input_changed_during_run_detected(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模拟运行期间 mtime 变化（注入式，不修改真实文件内容）。"""
    real = audit.file_identity
    calls = {"n": 0}

    def fake(path: Path) -> dict:
        calls["n"] += 1
        facts = real(path)
        if calls["n"] > 1:
            facts = {**facts, "mtime_ns": facts["mtime_ns"] + 1}
        return facts

    monkeypatch.setattr(audit, "file_identity", fake)
    allow = workdir
    code, evidence = _run(allow, _db(allow), allow / "out.json")
    assert code == 2
    assert evidence["status"] == "FAIL_INPUT_CHANGED"
    assert evidence["checks"]["input_unchanged"] is False


def test_d_sidecar_created_during_run_detected(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = audit.sidecars_of
    calls = {"n": 0}

    def fake(path: Path) -> list[str]:
        calls["n"] += 1
        return real(path) if calls["n"] == 1 else ["-wal"]

    monkeypatch.setattr(audit, "sidecars_of", fake)
    allow = workdir
    code, evidence = _run(allow, _db(allow), allow / "out.json")
    assert code == 2
    assert evidence["status"] == "FAIL_SIDECAR_CREATED"


# --------------------------------------------------------------------- E 只读模型


def test_e_query_only_enabled(workdir: Path) -> None:
    con = audit.open_snapshot(_db(workdir).resolve())
    try:
        assert con.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        con.close()


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO trade_plan (id) VALUES (1)",
        "UPDATE trade_plan SET id = 1",
        "DELETE FROM trade_plan",
        "CREATE TABLE evil (x INTEGER)",
        "DROP TABLE trade_plan",
        "ALTER TABLE trade_plan ADD COLUMN evil INTEGER",
        "ATTACH DATABASE 'other.db' AS other",
        "VACUUM",
    ],
)
def test_e_authorizer_denies_writes(workdir: Path, sql: str) -> None:
    con = audit.open_snapshot(_db(workdir).resolve())
    try:
        audit.install_authorizer(con)
        with pytest.raises(sqlite3.DatabaseError):
            con.execute(sql)
    finally:
        con.close()


@pytest.mark.parametrize(
    "sql,code",
    [
        ("INSERT INTO trade_plan (id) VALUES (1)", "REFUSE_SQL_HEAD"),
        ("VACUUM", "REFUSE_SQL_HEAD"),
        ("ATTACH DATABASE 'x.db' AS x", "REFUSE_SQL_HEAD"),
        ("PRAGMA writable_schema=1", "REFUSE_SQL_HEAD"),
        ("SELECT 1; DROP TABLE trade_plan", "REFUSE_MULTI_STATEMENT"),
        ("SELECT 1 /* DELETE */", "REFUSE_WRITE_KEYWORD"),
    ],
)
def test_e_safe_query_whitelist(workdir: Path, sql: str, code: str) -> None:
    con = audit.open_snapshot(_db(workdir).resolve())
    try:
        with pytest.raises(audit.AuditRefusal) as info:
            audit.safe_query(con, sql)
        assert info.value.code == code
    finally:
        con.close()


def test_e_safe_query_allows_select(workdir: Path) -> None:
    con = audit.open_snapshot(_db(workdir).resolve())
    try:
        assert audit.safe_query(con, "SELECT 1") == [(1,)]
    finally:
        con.close()


def test_e_pragma_whitelist(workdir: Path) -> None:
    con = audit.open_snapshot(_db(workdir).resolve())
    try:
        assert audit.read_pragma(con, "page_size")
        for bad in ("writable_schema", "mmap_size", "cell_size_check"):
            with pytest.raises(audit.AuditRefusal) as info:
                audit.read_pragma(con, bad)
            assert info.value.code == "REFUSE_PRAGMA"
    finally:
        con.close()


def test_e_extension_loading_disabled(workdir: Path) -> None:
    """`SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION=0` + authorizer 双重拦截。"""
    con = audit.open_snapshot(_db(workdir).resolve())
    try:
        audit.install_authorizer(con)
        with pytest.raises(sqlite3.OperationalError):
            con.load_extension("nonexistent_ext")
        with pytest.raises(sqlite3.OperationalError):
            con.execute("SELECT load_extension('nonexistent_ext')")
    finally:
        con.close()


# --------------------------------------------------------------------- F 输出


def test_f_extra_top_key_rejected() -> None:
    with pytest.raises(audit.AuditRefusal) as info:
        audit.assert_output_safe({"status": "PASS", "users": []})
    assert info.value.code == "FAIL_OUTPUT_WHITELIST_VIOLATION"


def test_f_extra_step_key_rejected() -> None:
    with pytest.raises(audit.AuditRefusal) as info:
        audit.assert_output_safe({"steps": {"trade_rows": 2}})
    assert info.value.code == "FAIL_OUTPUT_WHITELIST_VIOLATION"


def test_f_extra_probe_key_rejected() -> None:
    with pytest.raises(audit.AuditRefusal) as info:
        audit.assert_output_safe({"steps": {"probes": {"users": {"rows": 1}}}})
    assert info.value.code == "FAIL_OUTPUT_WHITELIST_VIOLATION"


@pytest.mark.parametrize(
    "payload",
    [
        {"steps": {"probes": {"trade_plan": {"username": "alice"}}}},
        {"steps": {"probes": {"trade_plan": {"note": "a@b.com"}}}},
        {"steps": {"probes": {"trade_plan": {"note": "call 13812345678"}}}},
        {"steps": {"probes": {"trade_plan": {"note": "-----BEGIN RSA PRIVATE KEY-----"}}}},
        {"steps": {"probes": {"trade_plan": {"note": "Bearer abcdefghijklmnopqrstuv"}}}},
        {"steps": {"probes": {"trade_plan": {"note": "x" * 600}}}},
    ],
)
def test_f_sensitive_content_rejected(payload: dict) -> None:
    with pytest.raises(audit.AuditRefusal) as info:
        audit.assert_output_safe(payload)
    assert info.value.code == "FAIL_OUTPUT_WHITELIST_VIOLATION"


def test_f_oversize_output_rejected() -> None:
    with pytest.raises(audit.AuditRefusal) as info:
        audit.assert_output_safe({"steps": {"table_names": ["t"] * 700000}})
    assert info.value.code == "FAIL_OUTPUT_TOO_LARGE"


def test_f_dynamic_table_names_allowed() -> None:
    """真实表名 user_session / 日期键不得被敏感词误判。"""
    audit.assert_output_safe(
        {
            "steps": {
                "row_counts": {"user_session": 3, "trade_journal": 2},
                "probes": {"rps_daily": {"rows_per_trading_day": {"2026-11-06": 5100}}},
            }
        }
    )


def test_f_sha_digest_value_not_flagged() -> None:
    audit.assert_output_safe({"input": {"sha256": "b2592acd77834711ee20c2137300349cfc1e90aa0021d575700c0cc28593a3ca"}})


# --------------------------------------------------------------------- G 资源


def test_g_timeout_guard() -> None:
    with pytest.raises(audit.AuditRefusal) as info:
        with audit.Guard(timeout_s=0, max_rss_mb=512, enforce=True) as guard:
            guard.checkpoint("test")
    assert info.value.code == "FAIL_TIMEOUT"


def test_g_rss_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit, "rss_peak_mb", lambda: 999.0)
    with pytest.raises(audit.AuditRefusal) as info:
        with audit.Guard(timeout_s=600, max_rss_mb=100, enforce=True) as guard:
            guard.checkpoint("test")
    assert info.value.code == "FAIL_RSS_EXCEEDED"


def test_g_guard_disabled_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit, "rss_peak_mb", lambda: 999.0)
    with audit.Guard(timeout_s=0, max_rss_mb=0, enforce=False) as guard:
        guard.checkpoint("test")


# --------------------------------------------------------------------- H 命令行


@pytest.mark.parametrize("flag", ["--force", "--ignore-safety", "--skip-checks", "--no-verify"])
def test_h_skip_flags_not_supported(flag: str) -> None:
    with pytest.raises(SystemExit):
        audit.build_parser().parse_args(
            [
                "--source-kind",
                "replica",
                "--db",
                "x.db",
                "--allow-dir",
                ".",
                "--out",
                "o.json",
                "--expect-sha256",
                "0" * 64,
                flag,
            ]
        )


def test_h_no_skip_option_in_parser() -> None:
    options = {opt for action in audit.build_parser()._actions for opt in action.option_strings}
    banned = {"--force", "--ignore-safety", "--skip-checks", "--no-verify", "--insecure"}
    assert not (options & banned)


# --------------------------------------------------------------------- I 冒烟


def test_i_replica_passes_but_never_real_prod(workdir: Path) -> None:
    allow = workdir
    schema_ref = Path.home() / "Downloads" / "prod_schema.json"
    over: dict = {"fingerprint": "trade_journal,trade_plan"}
    if schema_ref.exists():
        over["schema_ref"] = schema_ref
    code, evidence = _run(allow, _db(allow), allow / "out.json", **over)
    assert code == 0
    assert evidence["status"] == "PASS"
    assert evidence["real_prod_eligible"] is False
    checks = evidence["checks"]
    for name in (
        "query_only",
        "immutable",
        "defensive_mode",
        "extension_loading_disabled",
        "trusted_schema_off",
        "wal_header",
        "sidecars_absent",
        "not_production_path",
        "inside_allow_dir",
        "single_link",
        "quick_check_ok",
        "integrity_check_ok",
        "authorizer_installed",
        "input_unchanged",
        "no_side_files_created",
    ):
        assert checks[name] is True, name
    assert checks["posix_checks"] in {"enforced", "skipped_non_posix"}


def test_i_snapshot_kind_never_passes_on_non_posix(workdir: Path) -> None:
    if os.name == "posix":
        pytest.skip("本用例验证「非 POSIX 平台不得判定真实生产可用」")
    allow = workdir
    code, evidence = _run(allow, _db(allow), allow / "out.json", kind="snapshot")
    assert code == 2
    assert evidence["status"] == "FAIL_CHECK"
    assert evidence["real_prod_eligible"] is False


def test_i_fingerprint_returns_booleans_only(workdir: Path) -> None:
    report = audit.fingerprint_report(_db(workdir), ["trade_journal"])
    entry = report["trade_journal"]
    assert set(entry) == {
        "rows",
        "columns",
        "primary_key_present",
        "digest_stable",
        "elapsed_s",
    }
    assert entry["digest_stable"] is True
    assert not any(k.endswith("sha256") for k in entry)


def test_i_fingerprint_compare_returns_booleans_only(workdir: Path) -> None:
    other = workdir / "other.db"
    shutil.copyfile(_db(workdir), other)
    report = audit.fingerprint_compare(_db(workdir), other, ["trade_journal"])
    entry = report["trade_journal"]
    assert set(entry) <= {
        "rows_equal",
        "keys_equal",
        "values_equal",
        "status",
        "elapsed_s",
        "error",
    }
    assert entry["status"] == "same"
    assert "pk_sha256" not in entry and "content_sha256" not in entry
