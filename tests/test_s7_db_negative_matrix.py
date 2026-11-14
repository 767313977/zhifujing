"""S6（G7）：双库负向矩阵与 readiness 真实运行时检查。

覆盖任务书 §2.7 的关键场景（全部只对**临时库副本**执行，不碰生产库）：

- Research DB 的 INSERT/UPDATE/DELETE/DDL/写型 PRAGMA 与 ORM commit 全部被拒绝；
- 原生 sqlite3 以 `mode=ro` 打开同样写不进去；
- User DB 独立连接可写（`BEGIN IMMEDIATE` 探针），且探针会回滚、不留痕；
- Research DB 缺失 / User DB 缺失 / 两库同路径 / User DB 只读 / 研究文件可写
  时 readiness 的数据库门禁 **fail closed**；
- 拆分形态下 `session_scope()`（研究库写入口）直接拒绝；
- 健康形态下全部检查为 True（证明门禁不是恒 FAIL 的死代码）。
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app import db as db_module
from app import readiness
from app.db import ResearchDBReadOnlyError, sqlite_readonly_url
from app.models import Base


def _build_research(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO stock_daily(code, trade_date, close) "
                "VALUES ('000001', '2026-09-24', 10.0)"
            )
        )
    engine.dispose()


def _build_user(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO app_user(username, password_hash, is_admin, created_at) "
                "VALUES ('s7-user', 'x', 0, '2026-11-15 00:00:00')"
            )
        )
    engine.dispose()


def _set_readonly(path: Path) -> None:
    os.chmod(path, stat.S_IREAD)


def _set_writable(path: Path) -> None:
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)


def _readonly_engine(path: Path):
    engine = create_engine(sqlite_readonly_url(path), future=True)

    @event.listens_for(engine, "connect")
    def _query_only(dbapi_conn, _record):  # pragma: no cover - 事件回调
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA query_only=ON")
        cursor.close()

    return engine


@pytest.fixture()
def split_env(tmp_path):
    research = tmp_path / "research.db"
    user = tmp_path / "user.db"
    _build_research(research)
    _build_user(user)
    _set_readonly(research)
    try:
        yield research, user
    finally:
        # Windows 不允许删除只读文件：先恢复写权限
        _set_writable(research)
        if user.exists():
            _set_writable(user)


def _patch_readiness(monkeypatch, research: Path, user: Path, ro_engine) -> None:
    monkeypatch.setattr(
        readiness,
        "get_settings",
        lambda: SimpleNamespace(
            db_split_enabled=True, db_path=research, user_db_path=user
        ),
    )
    monkeypatch.setattr(db_module, "engine", ro_engine)
    monkeypatch.setattr(
        db_module,
        "ResearchSessionLocal",
        sessionmaker(bind=ro_engine, autoflush=False, expire_on_commit=False),
    )


def test_research_orm_commit_is_rejected(monkeypatch, split_env):
    research, user = split_env
    ro_engine = _readonly_engine(research)
    try:
        Session = sessionmaker(bind=ro_engine, autoflush=False, expire_on_commit=False)
        with Session() as session:
            with pytest.raises(OperationalError):
                session.execute(
                    text(
                        "INSERT INTO stock_daily(code, trade_date, close) "
                        "VALUES ('000002', '2026-09-25', 1.0)"
                    )
                )
                session.commit()
            session.rollback()
        # 原生 sqlite3 同样写不进去
        connection = sqlite3.connect(f"file:{research.as_posix()}?mode=ro", uri=True)
        try:
            with pytest.raises(sqlite3.OperationalError):
                connection.execute(
                    "INSERT INTO stock_daily(code, trade_date, close) "
                    "VALUES ('000003', '2026-09-26', 2.0)"
                )
        finally:
            connection.close()
    finally:
        ro_engine.dispose()


def test_user_db_probe_writes_nothing(tmp_path):
    user = tmp_path / "user.db"
    _build_user(user)
    ok, reason = readiness._user_db_writable(user)
    assert ok is True and reason is None
    with sqlite3.connect(user) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "__s6_write_probe" not in names, "可写探针必须回滚，不得留下表"


def test_database_gate_passes_only_on_healthy_split(monkeypatch, split_env):
    research, user = split_env
    ro_engine = _readonly_engine(research)
    try:
        _patch_readiness(monkeypatch, research, user, ro_engine)
        checks = readiness.database_checks()
        assert checks == {
            "SPLIT_ENABLED": True,
            "RESEARCH_DB_EXISTS": True,
            "RESEARCH_ENGINE_READONLY_URI": True,
            "QUERY_ONLY_ENABLED": True,
            "READONLY_WRITE_PROBE_BLOCKED": True,
            "RESEARCH_FILE_PERMISSION_READONLY": True,
            "USER_DB_EXISTS": True,
            "USER_DB_WRITABLE": True,
            "PATHS_DISTINCT": True,
        }
        assert readiness._database_gate(checks) == (readiness.PASS, None)
    finally:
        ro_engine.dispose()


def test_database_gate_fails_when_research_missing(monkeypatch, tmp_path):
    research = tmp_path / "missing.db"
    user = tmp_path / "user.db"
    _build_user(user)
    ro_engine = create_engine(sqlite_readonly_url(research), future=True)
    try:
        _patch_readiness(monkeypatch, research, user, ro_engine)
        state, reason = readiness._database_gate()
        assert state == readiness.FAIL
        assert reason == "RESEARCH_DB_MISSING"
    finally:
        ro_engine.dispose()


def test_database_gate_fails_when_user_missing(monkeypatch, split_env):
    research, _user = split_env
    missing_user = research.parent / "nope-user.db"
    ro_engine = _readonly_engine(research)
    try:
        _patch_readiness(monkeypatch, research, missing_user, ro_engine)
        state, reason = readiness._database_gate()
        assert state == readiness.FAIL
        assert reason == "USER_DB_MISSING"
    finally:
        ro_engine.dispose()


def test_database_gate_fails_when_paths_are_identical(monkeypatch, split_env):
    research, _user = split_env
    ro_engine = _readonly_engine(research)
    try:
        _patch_readiness(monkeypatch, research, research, ro_engine)
        state, reason = readiness._database_gate()
        assert state == readiness.FAIL
        assert reason == "DB_PATHS_NOT_DISTINCT"
    finally:
        ro_engine.dispose()


def test_database_gate_fails_when_user_db_readonly(monkeypatch, split_env):
    research, user = split_env
    _set_readonly(user)
    ro_engine = _readonly_engine(research)
    try:
        _patch_readiness(monkeypatch, research, user, ro_engine)
        state, reason = readiness._database_gate()
        assert state == readiness.FAIL
        assert reason == "USER_DB_NOT_WRITABLE"
    finally:
        ro_engine.dispose()


def test_database_gate_fails_when_research_file_writable(monkeypatch, split_env):
    research, user = split_env
    _set_writable(research)
    ro_engine = _readonly_engine(research)
    try:
        _patch_readiness(monkeypatch, research, user, ro_engine)
        state, reason = readiness._database_gate()
        assert state == readiness.FAIL
        assert reason == "RESEARCH_FILE_PERMISSION_WRITABLE"
    finally:
        ro_engine.dispose()


def test_database_gate_requires_split(monkeypatch, split_env):
    research, user = split_env
    monkeypatch.setattr(
        readiness,
        "get_settings",
        lambda: SimpleNamespace(
            db_split_enabled=False, db_path=research, user_db_path=user
        ),
    )
    state, reason = readiness._database_gate()
    assert state == readiness.FAIL
    assert reason == "DB_SPLIT_NOT_ACTIVE"


def test_research_write_entrypoint_rejects_when_split(monkeypatch):
    monkeypatch.setattr(db_module.settings, "db_split_enabled", True)
    with pytest.raises(ResearchDBReadOnlyError):
        with db_module.session_scope():
            pass


def test_file_readonly_detection_roundtrip(tmp_path):
    path = tmp_path / "x.db"
    path.write_bytes(b"")
    _set_readonly(path)
    assert readiness._file_is_readonly(path) is True
    _set_writable(path)
    assert readiness._file_is_readonly(path) is False
