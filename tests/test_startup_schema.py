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


# ---- P1.0-02：CLI / 脚本入口的隐式迁移闸 ----


def test_init_db_refuses_legacy_db_without_migrate(tmp_path, monkeypatch):
    """非空旧库 + verify：CLI 走 init_db 也必须失败，且不改库。"""
    path = tmp_path / "legacy.db"
    _old_db(path)
    monkeypatch.setattr(db, "engine", _engine(path))
    monkeypatch.setenv("FUPAN_SCHEMA_MODE", "verify")
    with pytest.raises(RuntimeError) as excinfo:
        db.init_db()
    assert "migrate_db.py" in str(excinfo.value)
    columns = {row[1] for row in sqlite3.connect(str(path)).execute("PRAGMA table_info(rps_daily)")}
    assert "ret20" not in columns
    tables = {
        row[0]
        for row in sqlite3.connect(str(path)).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert tables == {"rps_daily"}


def test_init_db_creates_fresh_database(tmp_path, monkeypatch):
    """空库属于「初始化」，verify 模式也允许建表（没有历史数据可破坏）。"""
    path = tmp_path / "fresh.db"
    monkeypatch.setattr(db, "engine", _engine(path))
    monkeypatch.setenv("FUPAN_SCHEMA_MODE", "verify")
    db.init_db()
    assert db.schema_report()["ok"] is True


def test_init_db_migrates_when_explicitly_allowed(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    _old_db(path)
    monkeypatch.setattr(db, "engine", _engine(path))
    monkeypatch.setenv("FUPAN_SCHEMA_MODE", "verify")
    db.init_db(allow_migrate=True)
    assert db.schema_report()["ok"] is True
    db.init_db(allow_migrate=True)  # 幂等


# ---- P1.3-R：生产两处结构漂移的回归测试 ----
#
# 生产库上这两列与候选模型声明不一致，但启动门禁是「按名字判断存在性」，
# 有意不比较 type / nullable / default（见 schema_report 文档）。下面把
# 「容忍」这件事固化成测试，避免以后有人误以为门禁会拦住它们，也避免
# 有人反过来去放宽门禁。

PROD_SECTOR_DAILY_DDL = """
CREATE TABLE sector_daily (
    trade_date DATE NOT NULL,
    sector_code VARCHAR(32) NOT NULL,
    name VARCHAR(64),
    taxonomy VARCHAR(16),
    pct_chg FLOAT,
    amount FLOAT,
    volume FLOAT,
    net_inflow FLOAT,
    up_count INTEGER,
    down_count INTEGER,
    member_count INTEGER,
    leader_name VARCHAR(32),
    leader_pct_chg FLOAT,
    source VARCHAR(16),
    strength FLOAT,
    PRIMARY KEY (trade_date, sector_code)
)
"""

PROD_TRADE_PLAN_DDL = """
CREATE TABLE trade_plan (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    code VARCHAR(16) NOT NULL,
    name VARCHAR(32),
    plan_date DATE NOT NULL,
    buy_price FLOAT NOT NULL,
    stop_price FLOAT NOT NULL,
    note TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    source VARCHAR(32),
    source_name VARCHAR(64),
    signal_date DATE,
    ref_price FLOAT,
    buy_high FLOAT,
    target_price FLOAT,
    position_note TEXT,
    observe_only BOOLEAN DEFAULT 0,
    status VARCHAR(16),
    status_source VARCHAR(8),
    actual_price FLOAT,
    followed_plan BOOLEAN,
    deviation VARCHAR(32)
)
"""


def test_declared_drift_is_tolerated_by_name_only_gate(tmp_path):
    """两处漂移不会导致启动拒绝，也不会被静默「修正」成模型声明。"""
    path = tmp_path / "prod_like.db"
    con = sqlite3.connect(str(path))
    con.execute(PROD_SECTOR_DAILY_DDL)
    con.execute(PROD_TRADE_PLAN_DDL)
    con.commit()
    con.close()

    engine = _engine(path)
    db.migrate_schema(engine)
    report = db.schema_report(engine)
    assert report["ok"] is True
    assert report["missing_columns"] == []

    con = sqlite3.connect(str(path))
    sector_cols = {row[1]: row for row in con.execute("PRAGMA table_info(sector_daily)")}
    plan_cols = {row[1]: row for row in con.execute("PRAGMA table_info(trade_plan)")}
    con.close()
    # 迁移只加列 / 建索引，不会重建表：生产 DDL 原样保留
    assert sector_cols["leader_name"][2] == "VARCHAR(32)"
    assert plan_cols["observe_only"][3] == 0 and plan_cols["observe_only"][4] == "0"

    # 模型声明的另一侧（用于固定「漂移」这个事实本身）
    from app.models import SectorDaily, TradePlan

    assert SectorDaily.__table__.c.leader_name.type.length == 128
    assert TradePlan.__table__.c.observe_only.nullable is False


def test_observe_only_null_reads_as_false_and_new_rows_write_zero(tmp_path):
    """NULL 与 0 语义等价：读取归一、新写入不再产生 NULL。"""
    from datetime import date

    from sqlalchemy.orm import Session

    from app.models import TradePlan
    from app.services.trade_plan import _auto_status

    path = tmp_path / "plan.db"
    con = sqlite3.connect(str(path))
    con.execute(PROD_TRADE_PLAN_DDL)
    con.execute(
        "INSERT INTO trade_plan(user_id, code, plan_date, buy_price, stop_price,"
        " created_at, updated_at, observe_only) VALUES"
        " (1, '600000', '2026-09-01', 10.0, 9.0, '2026-08-31 20:00:00',"
        " '2026-08-31 20:00:00', NULL)"
    )
    con.commit()
    con.close()

    engine = _engine(path)
    db.migrate_schema(engine)
    with Session(engine) as session:
        plan = session.get(TradePlan, 1)
        assert plan.observe_only is None  # 库里确实是 NULL
        assert bool(plan.observe_only) is False  # 读取路径归一
        assert _auto_status(plan, date(2026, 8, 31)) == "observe"  # 不抛异常

        new_plan = TradePlan(
            user_id=1, code="600001", plan_date=date(2026, 9, 2),
            buy_price=10.0, stop_price=9.0,
        )
        session.add(new_plan)
        session.commit()
        assert new_plan.observe_only is False
    engine.dispose()

    con = sqlite3.connect(str(path))
    stored = con.execute("SELECT observe_only, typeof(observe_only) FROM trade_plan WHERE code='600001'").fetchone()
    con.close()
    assert stored == (0, "integer")


def test_scheduler_not_started_when_schema_invalid(monkeypatch):
    """Schema 不兼容时，启动流程必须停在 ensure_schema，不能拉起采集调度器。"""
    import asyncio

    from app import main as main_module

    calls: list[str] = []
    monkeypatch.setattr(
        main_module, "ensure_schema", lambda: (_ for _ in ()).throw(RuntimeError("schema"))
    )
    monkeypatch.setattr(main_module, "start_scheduler", lambda: calls.append("start"))

    async def enter():
        async with main_module.lifespan(main_module.app):
            raise AssertionError("不该进入 lifespan 主体")

    with pytest.raises(RuntimeError, match="schema"):
        asyncio.run(enter())
    assert calls == []
