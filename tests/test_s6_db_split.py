"""S6.2：Research DB / User DB 拆分基础层测试。

覆盖：
- 只读 URI 拒绝 INSERT/UPDATE/DELETE/DDL；
- 用户表清单与 `init_user_db` 只建 10 张用户表；
- 迁移工具 dry-run 不写、apply 幂等、源库 SHA256 不变；
- 目标库只含用户表，研究表不进入 User DB；
- Research DB 缺失时 fail closed（连接直接失败）。
"""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app import db_roles
from app.db import sqlite_readonly_url
from app.models import AppUser, Base, StockDaily, Watchlist

ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_legacy_db(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            AppUser.__table__.insert().values(
                username="split-user", password_hash="x", is_admin=False
            )
        )
        user_id = connection.execute(text("SELECT id FROM app_user")).scalar()
        connection.execute(
            Watchlist.__table__.insert().values(user_id=user_id, code="600000")
        )
        connection.execute(
            StockDaily.__table__.insert().values(
                code="600000", trade_date=date(2026, 9, 24), close=10.5
            )
        )
    engine.dispose()


def test_readonly_uri_rejects_all_writes(tmp_path):
    db_path = tmp_path / "research.db"
    _make_legacy_db(db_path)
    ro_engine = create_engine(sqlite_readonly_url(db_path), future=True)
    with ro_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM stock_daily")).scalar() == 1
        for statement in (
            "INSERT INTO stock_daily(code, trade_date, close) VALUES ('000001','2026-09-24',1)",
            "UPDATE stock_daily SET close=1 WHERE 0",
            "DELETE FROM stock_daily WHERE 0",
            "CREATE TABLE __s6_probe (x INTEGER)",
            "PRAGMA user_version=7",
        ):
            with pytest.raises(OperationalError):
                connection.execute(text(statement))
    ro_engine.dispose()


def test_user_table_manifest_is_exactly_ten():
    tables = {table.name for table in db_roles.user_table_objects()}
    assert tables == {
        "app_user",
        "invite_code",
        "user_session",
        "watchlist",
        "review_note",
        "user_risk_pref",
        "trade_journal",
        "trade_t",
        "trade_plan",
        "analysis_snapshot",
    }
    assert db_roles.table_domain("stock_daily") == db_roles.RESEARCH
    assert db_roles.table_domain("watchlist") == db_roles.USER


def test_missing_research_db_fails_closed(tmp_path):
    missing = tmp_path / "nope.db"
    ro_engine = create_engine(sqlite_readonly_url(missing), future=True)
    with pytest.raises(OperationalError):
        with ro_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    ro_engine.dispose()
    assert not missing.exists()


def _run_migration(source: Path, target: Path, allow: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "s6_migrate_user_db.py"),
            "--source-copy",
            str(source),
            "--target-user-db",
            str(target),
            "--allow-dir",
            str(allow),
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_migration_dry_run_apply_and_idempotent(tmp_path):
    allow = tmp_path / "isolated"
    allow.mkdir()
    source = allow / "legacy-copy.db"
    target = allow / "user.db"
    _make_legacy_db(source)
    source_sha = _sha256(source)

    dry = _run_migration(source, target, allow, "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert not target.exists(), "dry-run 不得创建目标文件"
    assert _sha256(source) == source_sha

    apply1 = _run_migration(source, target, allow, "--apply")
    assert apply1.returncode == 0, apply1.stderr
    assert _sha256(source) == source_sha, "源库必须保持不变"

    with sqlite3.connect(target) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "app_user" in tables and "watchlist" in tables
        assert "stock_daily" not in tables, "研究表不得进入 User DB"
        assert connection.execute("SELECT COUNT(*) FROM app_user").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 1

    apply2 = _run_migration(source, target, allow, "--apply")
    assert apply2.returncode == 0, apply2.stderr
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT COUNT(*) FROM app_user").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 1


def test_migration_refuses_paths_outside_allow_dir(tmp_path):
    source = tmp_path / "legacy.db"
    _make_legacy_db(source)
    outside = tmp_path / "outside"
    outside.mkdir()
    result = _run_migration(source, outside / "user.db", tmp_path / "isolated", "--apply")
    assert result.returncode != 0
    assert "allow-dir" in result.stderr or "隔离目录" in result.stderr
