"""P0.9-08 启动与迁移解耦测试。

重点：普通启动**不得**执行 DDL；缺 Schema 时明确失败；显式迁移幂等；
只读连接上的检查不会尝试写入。
"""

import sqlite3

import pytest
from sqlalchemy import create_engine, event

from app import db

OLD_RPS_DDL = """
CREATE TABLE rps_daily (
    trade_date DATE NOT NULL,
    code VARCHAR(16) NOT NULL,
    ret120 FLOAT, ret250 FLOAT, rps120 FLOAT, rps250 FLOAT, ret50 FLOAT, rps50 FLOAT,
    PRIMARY KEY (trade_date, code)
)
"""


def _engine(path):
    return create_engine(f"sqlite:///{path}", future=True)


def _old_db(path) -> None:
    con = sqlite3.connect(str(path))
    con.execute(OLD_RPS_DDL)
    con.commit()
    con.close()


def test_verify_fails_on_old_schema_without_ddl(tmp_path):
    path = tmp_path / "old.db"
    _old_db(path)
    engine = _engine(path)
    with pytest.raises(RuntimeError) as excinfo:
        db.ensure_schema(mode="verify", target_engine=engine)
    message = str(excinfo.value)
    assert "rps_daily.ret20" in message
    assert "migrate_db.py" in message

    # 关键：失败之后库仍然是老 Schema —— 没有偷偷 DDL
    columns = {row[1] for row in sqlite3.connect(str(path)).execute("PRAGMA table_info(rps_daily)")}
    assert "ret20" not in columns
    tables = {
        row[0]
        for row in sqlite3.connect(str(path)).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert tables == {"rps_daily"}


def test_verify_never_calls_migrate(tmp_path, monkeypatch):
    path = tmp_path / "good.db"
    engine = _engine(path)
    db.migrate_schema(engine)  # 先建好当前 Schema
    calls: list[int] = []
    monkeypatch.setattr(db, "migrate_schema", lambda *a, **k: calls.append(1) or {})
    result = db.ensure_schema(mode="verify", target_engine=engine)
    assert result["mode"] == "verify"
    assert result["ok"] is True
    assert calls == []


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "old.db"
    _old_db(path)
    engine = _engine(path)
    first = db.ensure_schema(mode="migrate", target_engine=engine)
    assert "rps_daily.ret20" in first["columns"]
    second = db.ensure_schema(mode="migrate", target_engine=engine)
    assert second["columns"] == []
    assert second["indexes"] == []
    assert db.ensure_schema(mode="verify", target_engine=engine)["ok"] is True


def test_schema_report_is_read_only(tmp_path):
    path = tmp_path / "ro.db"
    engine = _engine(path)
    db.migrate_schema(engine)
    engine.dispose()

    readonly = _engine(path)

    @event.listens_for(readonly, "connect")
    def _query_only(conn, _record):
        conn.execute("PRAGMA query_only=1")

    report = db.schema_report(readonly)
    assert report["ok"] is True
    assert report["missing_tables"] == []


def test_schema_mode_env_override(monkeypatch):
    monkeypatch.setenv("FUPAN_SCHEMA_MODE", "migrate")
    assert db.schema_mode() == "migrate"
    monkeypatch.setenv("FUPAN_SCHEMA_MODE", "verify")
    assert db.schema_mode() == "verify"
    monkeypatch.setenv("FUPAN_SCHEMA_MODE", "bogus")
    with pytest.raises(RuntimeError):
        db.schema_mode()


def test_main_lifespan_uses_ensure_schema_not_init_db():
    """源码级约束：启动链路不能再直接调 `init_db()`（否则又变成隐式迁移）。"""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "backend" / "app" / "main.py").read_text(
        encoding="utf-8"
    )
    assert "ensure_schema()" in source
    # 只允许出现在注释里的 `init_db()`：真正的调用行必须是 4 空格缩进的语句
    assert "\n    init_db()\n" not in source
