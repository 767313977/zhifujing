"""P0.9-06 RPS 历史导入：逐字段合并、冲突、幂等与回滚。

脚本不在 `app` 包内（运维脚本），用 importlib 按路径加载它。
"""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "import_rps_history", ROOT / "scripts" / "import_rps_history.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


MODULE = _load_script()

DDL = """
CREATE TABLE rps_daily (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    ret120 FLOAT, ret250 FLOAT, rps120 FLOAT, rps250 FLOAT, ret50 FLOAT, rps50 FLOAT,
    ret20 FLOAT, ret60 FLOAT, rps20 FLOAT, rps60 FLOAT,
    PRIMARY KEY (trade_date, code)
)
"""


def _db(path, rows=()):
    con = sqlite3.connect(str(path))
    con.execute(DDL)
    con.executemany(
        "INSERT INTO rps_daily (trade_date, code, ret120, ret250, rps120, rps250,"
        " ret50, rps50, ret20, ret60, rps20, rps60)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()


def _connect(source: Path, target: Path):
    con = sqlite3.connect(str(target), isolation_level=None, uri=True)
    con.execute("ATTACH DATABASE ? AS src", (f"file:{source.as_posix()}?mode=ro",))
    return con


@pytest.fixture()
def pair(tmp_path):
    source = tmp_path / "src.db"
    target = tmp_path / "tgt.db"
    _db(
        source,
        [
            # 只有 20 日：应新增
            ("2026-10-08", "000001", 10, 20, 30, 40, None, None, 5.0, None, 70.0, None),
            # 四列全空：不导入
            ("2026-10-08", "000002", 11, 21, 31, 41, None, None, None, None, None, None),
            # 目标已有该主键、20 日为空：补齐
            ("2026-10-08", "000003", 12, 22, 32, 42, None, None, 3.0, None, 66.0, None),
            # 目标已有值且来源不同：冲突，不覆盖
            ("2026-10-08", "000004", 13, 23, 33, 43, None, None, 9.0, 8.0, 90.0, 91.0),
            # 目标已有值且来源一致：幂等
            ("2026-10-08", "000005", 14, 24, 34, 44, None, None, 1.0, None, 50.0, None),
        ],
    )
    _db(
        target,
        [
            # 旧列与来源不同：导入不得改动旧列
            ("2026-10-08", "000003", 99, 99, 99, 99, 1.0, 1.0, None, None, None, None),
            ("2026-10-08", "000004", 1, 2, 3, 4, 1.0, 1.0, 1.0, None, 10.0, None),
            ("2026-10-08", "000005", 1, 2, 3, 4, 1.0, 1.0, 1.0, None, 50.0, None),
            # 孤立的旧行（来源没有）：保持不动
            ("2026-10-09", "000009", 1, 2, 3, 4, 1.0, 1.0, None, None, None, None),
        ],
    )
    con = _connect(source, target)
    yield con
    con.close()


def test_summary_counts_candidates_and_fills(pair):
    summary = MODULE._summary(pair, None, None)
    total = summary["total"]
    assert total["source_rows"] == 4  # 000002 四列全空被排除
    assert total["missing_keys"] == 1  # 000001
    assert total["fill_ret20"] == 1  # 000003
    assert total["fill_rps20"] == 1
    assert total["fill_ret60"] == 1  # 000004 的 ret60 目标为空
    assert total["fill_rps60"] == 1
    assert total["conflicts"] == 1  # 000004 的 ret20/rps20 不同


def test_apply_inserts_fills_and_keeps_old_columns(pair):
    result = MODULE._apply(pair, None, None)
    assert result["inserted"] == 1

    rows = {
        (row[0], row[1]): row
        for row in pair.execute(
            "SELECT trade_date, code, ret50, rps50, ret20, ret60, rps20, rps60, ret120 FROM main.rps_daily"
        )
    }
    # 新增：旧列与新列都来自同一份来源
    assert rows[("2026-10-08", "000001")][4] == 5.0
    assert rows[("2026-10-08", "000001")][6] == 70.0
    # 补齐：目标原本空的新列
    assert rows[("2026-10-08", "000003")][4] == 3.0
    # 冲突：目标值保留，来源值不覆盖
    assert rows[("2026-10-08", "000004")][4] == 1.0
    assert rows[("2026-10-08", "000004")][5] == 8.0  # 目标为空 → 补来源
    assert rows[("2026-10-08", "000004")][6] == 10.0
    # 旧列：000003 目标 ret50/rps50 = 1.0，来源是 99/99，不得改
    assert rows[("2026-10-08", "000003")][2] == 1.0
    assert rows[("2026-10-08", "000003")][3] == 1.0
    # 来源没有的孤立旧行保持不动
    assert rows[("2026-10-09", "000009")][4] is None
    # 四列全空的行没有被导入
    assert ("2026-10-08", "000002") not in rows


def test_apply_is_idempotent(pair):
    MODULE._apply(pair, None, None)
    before = pair.execute("SELECT COUNT(*) FROM main.rps_daily").fetchone()[0]
    summary = MODULE._summary(pair, None, None)
    assert summary["total"]["missing_keys"] == 0
    assert summary["total"]["fill_ret20"] == 0
    assert summary["total"]["fill_ret60"] == 0
    assert summary["total"]["fill_rps20"] == 0
    assert summary["total"]["fill_rps60"] == 0

    second = MODULE._apply(pair, None, None)
    assert second["inserted"] == 0
    after = pair.execute("SELECT COUNT(*) FROM main.rps_daily").fetchone()[0]
    assert after == before


def test_apply_rolls_back_on_error(pair):
    before = pair.execute("SELECT COUNT(*) FROM main.rps_daily").fetchone()[0]
    pair.execute(
        "CREATE TRIGGER boom BEFORE UPDATE ON main.rps_daily"
        " BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        MODULE._apply(pair, None, None)
    after = pair.execute("SELECT COUNT(*) FROM main.rps_daily").fetchone()[0]
    assert after == before  # 先插入的行也一起回滚


def test_null_is_not_treated_as_zero(tmp_path):
    """零收益必须原样保留为 0，不能被当成缺失。"""
    source = tmp_path / "src_zero.db"
    target = tmp_path / "tgt_zero.db"
    _db(source, [("2026-10-10", "000010", None, None, None, None, None, None, 0.0, None, 0.0, None)])
    _db(target)
    con = _connect(source, target)
    try:
        MODULE._apply(con, None, None)
        row = con.execute(
            "SELECT ret20, rps20 FROM main.rps_daily WHERE code='000010' AND trade_date='2026-10-10'"
        ).fetchone()
    finally:
        con.close()
    assert row == (0.0, 0.0)


def test_range_limits_import(pair):
    summary = MODULE._summary(pair, "2026-10-09", None)
    assert summary["total"]["source_rows"] == 0
    result = MODULE._apply(pair, "2026-10-09", None)
    assert result["inserted"] == 0
