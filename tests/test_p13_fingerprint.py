"""P1.3-R2 R-01 回归：迁移数据指纹必须能检出「原有字段被改写」。

旧实现按 `rowid % 100 = 7` 抽样，改写非抽样行时会漏判。这里的用例全部使用
小表（每行都会被摘要），并显式验证：

- 原有字段改写 → projection / full 摘要变化；
- 只新增 NULL 列 → projection 不变、full 变化；
- 行插入顺序不影响摘要（按主键排序）；
- NULL、0、空串互不混淆；
- 删除 / 主键改写被主键与行数校验检出；
- Schema 变化清单能列出新增表 / 列 / 索引 / 触发器。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.p13_fingerprint import (  # noqa: E402
    compare_fingerprint,
    fingerprint_table,
    schema_diff,
    schema_snapshot,
)


def _make_db(path: Path, rows: list[tuple], *, extra_column: bool = False) -> None:
    con = sqlite3.connect(str(path))
    try:
        if extra_column:
            con.execute(
                "CREATE TABLE t (id INTEGER PRIMARY KEY, code TEXT, price REAL, note TEXT)"
            )
        else:
            con.execute(
                "CREATE TABLE t (id INTEGER PRIMARY KEY, code TEXT, price REAL)"
            )
        for index, (code, price, *rest) in enumerate(rows, start=1):
            if extra_column:
                con.execute(
                    "INSERT INTO t (id, code, price, note) VALUES (?,?,?,?)",
                    (index, code, price, rest[0] if rest else None),
                )
            else:
                con.execute(
                    "INSERT INTO t (id, code, price) VALUES (?,?,?)",
                    (index, code, price),
                )
        con.commit()
    finally:
        con.close()


def _rewrite(path: Path, row_id: int, **values) -> None:
    con = sqlite3.connect(str(path))
    try:
        for column, value in values.items():
            con.execute(f"UPDATE t SET {column}=? WHERE id=?", (value, row_id))
        con.commit()
    finally:
        con.close()


def test_original_field_rewrite_is_detected(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    _make_db(db, [("600000", 10.5), ("000001", 12.25), ("300750", 200.0)])
    before = fingerprint_table(db, "t")
    _rewrite(db, 2, price=12.26)
    after = fingerprint_table(db, "t")

    result = compare_fingerprint(before, after)
    assert result["rows_match"] is True
    assert result["pk_sha256_match"] is True
    assert result["content_sha256_match"] is False
    assert result["unchanged"] is False


def test_new_null_column_changes_full_but_not_projection(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    _make_db(db, [("600000", 10.5), ("000001", 12.25)])
    before = fingerprint_table(db, "t")

    con = sqlite3.connect(str(db))
    try:
        con.execute("ALTER TABLE t ADD COLUMN new_col TEXT")
        con.commit()
    finally:
        con.close()

    projected = fingerprint_table(db, "t", before["columns"])
    full = fingerprint_table(db, "t")

    assert compare_fingerprint(before, projected)["unchanged"] is True
    assert compare_fingerprint(before, full)["content_sha256_match"] is False
    assert full["columns"] == ["id", "code", "price", "new_col"]


def test_row_order_does_not_change_digest(tmp_path: Path) -> None:
    one = tmp_path / "one.db"
    two = tmp_path / "two.db"
    rows = [(1, "600000", 10.5), (2, "000001", 12.25), (3, "300750", 200.0)]
    for path, order in ((one, rows), (two, list(reversed(rows)))):
        con = sqlite3.connect(str(path))
        try:
            con.execute(
                "CREATE TABLE t (id INTEGER PRIMARY KEY, code TEXT, price REAL)"
            )
            con.executemany("INSERT INTO t (id, code, price) VALUES (?,?,?)", order)
            con.commit()
        finally:
            con.close()
    assert fingerprint_table(one, "t") == fingerprint_table(two, "t")


def test_null_zero_and_empty_string_are_distinct(tmp_path: Path) -> None:
    null_db = tmp_path / "null.db"
    zero_db = tmp_path / "zero.db"
    empty_db = tmp_path / "empty.db"
    _make_db(null_db, [(None, 10.5)])
    _make_db(zero_db, [(0, 10.5)])
    _make_db(empty_db, [("", 10.5)])

    digests = {
        name: fingerprint_table(path, "t")["content_sha256"]
        for name, path in (
            ("null", null_db),
            ("zero", zero_db),
            ("empty", empty_db),
        )
    }
    assert len(set(digests.values())) == 3


def test_numeric_and_datetime_normalization(tmp_path: Path) -> None:
    int_db = tmp_path / "int.db"
    float_db = tmp_path / "float.db"
    _make_db(int_db, [("600000", 10)])
    _make_db(float_db, [("600000", 10.0)])

    assert (
        fingerprint_table(int_db, "t")["content_sha256"]
        == fingerprint_table(float_db, "t")["content_sha256"]
    )

    time_db = tmp_path / "time.db"
    con = sqlite3.connect(str(time_db))
    try:
        con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, ts TEXT)")
        con.execute("INSERT INTO t VALUES (1, '2026-11-09 12:00:00.000000')")
        con.commit()
    finally:
        con.close()
    other = tmp_path / "time2.db"
    con = sqlite3.connect(str(other))
    try:
        con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, ts TEXT)")
        con.execute("INSERT INTO t VALUES (1, '2026-11-09T12:00:00')")
        con.commit()
    finally:
        con.close()
    assert (
        fingerprint_table(time_db, "t")["content_sha256"]
        == fingerprint_table(other, "t")["content_sha256"]
    )


def test_row_deletion_and_pk_rewrite_detected(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    _make_db(db, [("600000", 10.5), ("000001", 12.25)])
    before = fingerprint_table(db, "t")

    con = sqlite3.connect(str(db))
    try:
        con.execute("DELETE FROM t WHERE id=2")
        con.commit()
    finally:
        con.close()

    deleted = compare_fingerprint(before, fingerprint_table(db, "t"))
    assert deleted["rows_match"] is False
    assert deleted["pk_sha256_match"] is False
    assert deleted["unchanged"] is False

    rewrite_db = tmp_path / "b.db"
    _make_db(rewrite_db, [("600000", 10.5), ("000001", 12.25)])
    con = sqlite3.connect(str(rewrite_db))
    try:
        con.execute("UPDATE t SET id=99 WHERE id=1")
        con.commit()
    finally:
        con.close()
    moved = compare_fingerprint(before, fingerprint_table(rewrite_db, "t"))
    assert moved["rows_match"] is True
    assert moved["pk_sha256_match"] is False
    assert moved["unchanged"] is False


def test_schema_diff_lists_added_objects(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    _make_db(db, [("600000", 10.5)])
    before = schema_snapshot(db)

    con = sqlite3.connect(str(db))
    try:
        con.execute("ALTER TABLE t ADD COLUMN extra TEXT")
        con.execute("CREATE INDEX ix_t_code ON t(code)")
        con.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
        con.execute(
            "CREATE TRIGGER trg_t_ins AFTER INSERT ON t BEGIN "
            "UPDATE t SET code=code WHERE id=NEW.id; END"
        )
        con.commit()
    finally:
        con.close()

    after = schema_snapshot(db)
    diff = schema_diff(before, after)

    assert diff["tables_added"] == ["other"]
    assert diff["columns_added"]["t"] == ["extra"]
    assert diff["indexes_added"] == ["ix_t_code"]
    assert diff["triggers_added"] == ["trg_t_ins"]
    assert diff["tables_before"] == 1
    assert diff["tables_after"] == 2
