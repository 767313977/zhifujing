"""install_data_revision.py 的 CLI 与安全边界测试。

覆盖：
- 主库默认拒绝、`--allow-main-db` 放行、`--status` 只读；
- Schema 校验、安装后触发器核对（名称 / 绑定表 / 事件 / SQL 正文）；
- **生产严格口径**：8 个数据域 / 24 条齐全，缺一张域表就拒绝；
- 本地/隔离库按现有表安装，但输出必须标明「不代表生产验收」；
- 重复安装不换 db_uuid、不清 revision、不删数据；
- `--status` 对不存在的路径不创建空库；
- 每种事件（INSERT/UPDATE/DELETE）在测试事务里都能推进对应 revision。

所有测试用临时库，不触碰 backend/data/fupan.db。
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import Base  # noqa: E402
from app.services import data_revision  # noqa: E402

SCRIPT = ROOT / "scripts" / "install_data_revision.py"

#: `sector_member_backfill` 没有 SQLAlchemy 模型（手工建表，生产已有 1.6 万行）。
#: 测试库用最小列建出来，严格校验只要求这张表存在。
_BACKFILL_DDL = """
CREATE TABLE IF NOT EXISTS sector_member_backfill (
    trade_date DATE NOT NULL,
    sector_code TEXT NOT NULL,
    status TEXT,
    PRIMARY KEY (trade_date, sector_code)
)
"""


def _run(*extra: str, db: Path) -> subprocess.CompletedProcess:
    """以子进程跑脚本，返回 CompletedProcess（不抛异常）。"""
    cmd = [sys.executable, str(SCRIPT), "--db", str(db), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def _make_schema_db(path: Path) -> Path:
    """建一个带完整 Schema 的空库（8 个数据域的表齐全）。"""
    engine = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(_BACKFILL_DDL))
    engine.dispose()
    return path


def _trigger_count(db: Path) -> int:
    engine = create_engine(f"sqlite:///{db}", future=True)
    with Session(engine) as session:
        value = session.connection().exec_driver_sql(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger'"
        ).scalar()
    engine.dispose()
    return int(value)


def _extract_uuid(stdout: str) -> str:
    for line in stdout.splitlines():
        if "db_uuid=" in line:
            return line.split("db_uuid=", 1)[1].strip()
    raise AssertionError(f"未在输出中找到 db_uuid：{stdout}")


def test_install_creates_24_triggers(tmp_path):
    db = _make_schema_db(tmp_path / "rev.db")
    result = _run("--strict", db=db)
    assert result.returncode == 0, result.stderr
    assert "触发器严格校验通过：8 个数据域 / 24 条" in result.stdout
    assert "db_uuid=" in result.stdout
    assert _trigger_count(db) == 24

    engine = create_engine(f"sqlite:///{db}", future=True)
    with Session(engine) as session:
        names = {
            row[0]
            for row in session.connection().exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
    engine.dispose()
    expected = {
        data_revision.trigger_name(table, suffix)
        for tables_for_domain in data_revision.DOMAINS.values()
        for table in tables_for_domain
        for suffix in data_revision._SUFFIXES
    }
    assert expected == names


def test_install_is_idempotent_same_uuid(tmp_path):
    """重复安装：db_uuid 不变、revision 不清零、既有业务数据不丢、触发器仍是 24 条。"""
    db = _make_schema_db(tmp_path / "rev.db")
    first = _run("--strict", db=db)
    assert first.returncode == 0, first.stderr
    uuid_first = _extract_uuid(first.stdout)

    engine = create_engine(f"sqlite:///{db}", future=True)
    with Session(engine) as session:
        connection = session.connection()
        connection.exec_driver_sql(
            "INSERT INTO trade_calendar(trade_date) VALUES ('2026-01-05')"
        )
        connection.exec_driver_sql(
            "INSERT INTO data_revision(domain, revision) VALUES ('stock_daily', 7)"
        )
        session.commit()
    engine.dispose()

    second = _run("--strict", db=db)
    assert second.returncode == 0, second.stderr
    assert _extract_uuid(second.stdout) == uuid_first
    assert _trigger_count(db) == 24

    engine = create_engine(f"sqlite:///{db}", future=True)
    with Session(engine) as session:
        revision = session.connection().exec_driver_sql(
            "SELECT revision FROM data_revision WHERE domain='stock_daily'"
        ).scalar()
        calendar = session.connection().exec_driver_sql(
            "SELECT count(*) FROM trade_calendar WHERE trade_date='2026-01-05'"
        ).scalar()
    engine.dispose()
    assert revision == 7
    assert calendar == 1


def test_status_readonly_does_not_modify(tmp_path):
    db = _make_schema_db(tmp_path / "rev.db")
    assert _run("--strict", db=db).returncode == 0
    result = _run("--status", db=db)
    assert result.returncode == 0
    assert "db_uuid=" in result.stdout
    assert "已安装" not in result.stdout
    assert _trigger_count(db) == 24


def test_status_on_missing_path_does_not_create_db(tmp_path):
    """--status 不能因为「看一眼」就创建空库。"""
    missing = tmp_path / "nope.db"
    result = _run("--status", db=missing)
    assert result.returncode != 0
    assert "目标库不存在" in result.stderr
    assert not missing.exists()


def test_schema_missing_is_rejected(tmp_path):
    """空库（无业务表）→ Schema 校验失败，不装触发器。"""
    db = tmp_path / "empty.db"
    engine = create_engine(f"sqlite:///{db}", future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE foo (x INTEGER)"))
    engine.dispose()
    result = _run(db=db)
    assert result.returncode != 0
    assert "Schema 校验未通过" in result.stderr
    assert _trigger_count(db) == 0


def test_partial_db_tolerated_without_strict(tmp_path):
    """本地/隔离库缺域表：按现有表安装，但必须标明不代表生产验收。"""
    db = _make_schema_db(tmp_path / "partial.db")
    engine = create_engine(f"sqlite:///{db}", future=True)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE sector_member_backfill"))
    engine.dispose()

    result = _run(db=db)
    assert result.returncode == 0, result.stderr
    assert "触发器核对通过：21 条" in result.stdout
    assert "不代表生产验收" in result.stdout
    assert _trigger_count(db) == 21


def test_strict_rejects_missing_domain_table(tmp_path):
    """生产严格口径：缺 1 张域表 → 直接拒绝，不能按「现有表 × 3」放行。"""
    db = _make_schema_db(tmp_path / "partial.db")
    engine = create_engine(f"sqlite:///{db}", future=True)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE sector_member_backfill"))
    engine.dispose()

    result = _run("--strict", db=db)
    assert result.returncode != 0
    assert "sector_member_backfill" in result.stderr
    assert "8 个数据域齐全" in result.stderr
    assert _trigger_count(db) == 0


def test_strict_rejects_wrong_trigger_definition(tmp_path):
    """同名触发器但事件不对 → 严格校验必须抓到。"""
    db = _make_schema_db(tmp_path / "rev.db")
    assert _run("--strict", db=db).returncode == 0

    engine = create_engine(f"sqlite:///{db}", future=True)
    with Session(engine) as session:
        connection = session.connection()
        connection.exec_driver_sql("DROP TRIGGER trg_stock_daily_au")
        connection.exec_driver_sql(
            "CREATE TRIGGER trg_stock_daily_au AFTER INSERT ON stock_daily "
            "BEGIN INSERT INTO data_revision(domain, revision) VALUES ('stock_daily', 1) "
            "ON CONFLICT(domain) DO UPDATE SET revision = revision + 1; END"
        )
        session.commit()
    engine.dispose()

    result = _run("--strict", db=db)
    assert result.returncode != 0
    assert "不是 AFTER UPDATE ON stock_daily" in result.stderr


def test_strict_rejects_wrong_domain(tmp_path):
    """触发器写错了数据域 → 严格校验必须抓到。"""
    db = _make_schema_db(tmp_path / "rev.db")
    assert _run("--strict", db=db).returncode == 0

    engine = create_engine(f"sqlite:///{db}", future=True)
    with Session(engine) as session:
        connection = session.connection()
        connection.exec_driver_sql("DROP TRIGGER trg_rps_daily_ai")
        connection.exec_driver_sql(
            "CREATE TRIGGER trg_rps_daily_ai AFTER INSERT ON rps_daily "
            "BEGIN INSERT INTO data_revision(domain, revision) VALUES ('stock_daily', 1) "
            "ON CONFLICT(domain) DO UPDATE SET revision = revision + 1; END"
        )
        session.commit()
    engine.dispose()

    result = _run("--strict", db=db)
    assert result.returncode != 0
    assert "的数据域不是 rps" in result.stderr


def test_main_db_refused_without_flag(tmp_path, monkeypatch):
    """默认拒绝主库：_is_main_db 返回 True 且无 --allow-main-db → 退出非 0。"""
    db = _make_schema_db(tmp_path / "rev.db")
    monkeypatch.setattr("scripts.install_data_revision._is_main_db", lambda p: True)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--db", str(db)])
    import scripts.install_data_revision as mod

    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code != 0
    assert "拒绝修改主库" in str(exc.value)
    # 拒绝发生在任何写操作之前
    assert _trigger_count(db) == 0


def test_main_db_allowed_with_flag_is_strict(tmp_path, monkeypatch):
    """显式 --allow-main-db 放行，但主库自动走严格口径（8 域 / 24 条）。"""
    db = _make_schema_db(tmp_path / "rev.db")
    monkeypatch.setattr("scripts.install_data_revision._is_main_db", lambda p: True)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--db", str(db), "--allow-main-db"])
    import scripts.install_data_revision as mod

    assert mod.main() == 0
    assert _trigger_count(db) == 24


def test_main_db_with_partial_schema_is_rejected(tmp_path, monkeypatch):
    """主库缺域表时，即使加了 --allow-main-db 也不能「部分安装算完成」。"""
    db = _make_schema_db(tmp_path / "rev.db")
    engine = create_engine(f"sqlite:///{db}", future=True)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE sector_member_backfill"))
    engine.dispose()

    monkeypatch.setattr("scripts.install_data_revision._is_main_db", lambda p: True)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--db", str(db), "--allow-main-db"])
    import scripts.install_data_revision as mod

    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert "8 个数据域齐全" in str(exc.value)


def test_status_on_main_db_allowed_without_flag(tmp_path, monkeypatch):
    """--status 是只读，主库也不需要 --allow-main-db。"""
    db = _make_schema_db(tmp_path / "rev.db")
    monkeypatch.setattr("scripts.install_data_revision._is_main_db", lambda p: True)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--db", str(db), "--allow-main-db"])
    import scripts.install_data_revision as mod

    assert mod.main() == 0
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--db", str(db), "--status"])
    assert mod.main() == 0


def test_verify_triggers_catches_missing(tmp_path):
    """_verify_triggers 在缺触发器时抛 SystemExit（宽容口径也要抓到）。"""
    import scripts.install_data_revision as mod

    db = _make_schema_db(tmp_path / "rev.db")
    engine = create_engine(f"sqlite:///{db}", future=True)
    with Session(engine) as session:
        data_revision.install(session)
        session.connection().exec_driver_sql("DROP TRIGGER trg_stock_daily_ai")
        session.commit()
        with pytest.raises(SystemExit) as exc:
            mod._verify_triggers(session)
        assert "触发器核对失败" in str(exc.value)
        assert "trg_stock_daily_ai" in str(exc.value)
    engine.dispose()


# ---------------------------------------------------------------------------
# 动态验收：每种事件在测试事务里都能推进对应 revision
# ---------------------------------------------------------------------------


def _sample_value(declared: str):
    """按列声明类型造一个能插进去的最小值（只为触发 DML，不校验业务含义）。"""
    declared = (declared or "").upper()
    if "INT" in declared:
        return 1
    if "REAL" in declared or "FLOA" in declared or "DOUB" in declared:
        return 1.0
    if "DATE" in declared or "TIME" in declared:
        return "2026-01-05"
    return "x"


def _insert_row(connection, table: str) -> None:
    """插一行「只有主键 + NOT NULL 列」的最小行。"""
    info = connection.execute(f"PRAGMA table_info({table})").fetchall()
    values = {
        name: _sample_value(declared)
        for _, name, declared, notnull, default, pk in info
        if pk or (notnull and default is None)
    }
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", list(values.values()))


def _first_primary_key(connection, table: str) -> tuple[str, str]:
    """返回 (主键列名, 声明类型)。"""
    for _, name, declared, _, _, pk in connection.execute(
        f"PRAGMA table_info({table})"
    ).fetchall():
        if pk:
            return name, declared
    raise AssertionError(f"{table} 没有主键")


def _revision(connection, domain: str) -> int:
    row = connection.execute(
        "SELECT revision FROM data_revision WHERE domain = ?", (domain,)
    ).fetchone()
    return int(row[0]) if row else 0


def test_trigger_events_advance_revision(tmp_path):
    """8 张域表 × INSERT/UPDATE/DELETE：每次事件把该域 revision 推进 1，事务回滚。"""
    db = _make_schema_db(tmp_path / "rev.db")
    assert _run("--strict", db=db).returncode == 0

    connection = sqlite3.connect(str(db), isolation_level=None)
    try:
        for domain, tables in data_revision.DOMAINS.items():
            for table in tables:
                pk_name, pk_declared = _first_primary_key(connection, table)
                for action in ("INSERT", "UPDATE", "DELETE"):
                    connection.execute("SAVEPOINT revcheck")
                    try:
                        if action != "INSERT":
                            _insert_row(connection, table)
                        base = _revision(connection, domain)
                        if action == "INSERT":
                            _insert_row(connection, table)
                        elif action == "UPDATE":
                            connection.execute(
                                f"UPDATE {table} SET {pk_name} = {pk_name} "
                                f"WHERE {pk_name} = ?",
                                (_sample_value(pk_declared),),
                            )
                        else:
                            connection.execute(
                                f"DELETE FROM {table} WHERE {pk_name} = ?",
                                (_sample_value(pk_declared),),
                            )
                        assert _revision(connection, domain) == base + 1, (
                            f"{table} {action} 未推进 {domain} 的 revision"
                        )
                    finally:
                        connection.execute("ROLLBACK TO revcheck")
                        connection.execute("RELEASE revcheck")
    finally:
        connection.close()
