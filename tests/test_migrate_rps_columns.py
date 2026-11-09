"""B0.3 `rps_daily` 字段迁移 / 回滚：幂等、旧数据保留。"""

import importlib.util
import sqlite3
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_rps_columns.py"
spec = importlib.util.spec_from_file_location("migrate_rps_columns", SCRIPT)
migrate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migrate)


def _db(tmp_path):
    path = tmp_path / "rps.db"
    con = sqlite3.connect(str(path))
    con.execute(
        """
        CREATE TABLE rps_daily (
            trade_date DATE NOT NULL,
            code VARCHAR(16) NOT NULL,
            ret50 FLOAT, ret120 FLOAT, ret250 FLOAT,
            rps50 FLOAT, rps120 FLOAT, rps250 FLOAT,
            PRIMARY KEY (trade_date, code)
        )
        """
    )
    con.execute(
        "INSERT INTO rps_daily VALUES ('2026-10-09','000001',1.0,2.0,3.0,10.0,20.0,30.0)"
    )
    con.commit()
    return path, con


def test_apply_is_idempotent_and_rollback_keeps_old_columns(tmp_path):
    _path, con = _db(tmp_path)
    assert migrate._columns(con) == [
        "trade_date",
        "code",
        "ret50",
        "ret120",
        "ret250",
        "rps50",
        "rps120",
        "rps250",
    ]
    migrate.apply(con)
    migrate.apply(con)  # 第二次应是 no-op
    after = migrate._columns(con)
    for column in migrate.NEW_COLUMNS:
        assert column in after
    row = con.execute("select ret50, rps250 from rps_daily").fetchone()
    assert row == (1.0, 30.0)

    migrate.rollback(con)
    assert migrate._columns(con) == [
        "trade_date",
        "code",
        "ret50",
        "ret120",
        "ret250",
        "rps50",
        "rps120",
        "rps250",
    ]
    row = con.execute("select ret50, rps250 from rps_daily").fetchone()
    assert row == (1.0, 30.0)
    con.close()
