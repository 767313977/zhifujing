"""P1.3-R2 迁移数据指纹（R-01 修复）。

旧实现（`backend/data/p13_audit/stage6_migration.py` 的 `table_fingerprint`）有两个缺陷：

1. 按 `rowid % 100 = 7` 抽样，**原有字段被改写时可能整表漏判**；
2. 摘要把「迁移新增列」一起哈希，新增列一旦出现，摘要必然变化，
   无法区分「数据被改」与「Schema 扩展」，于是 `fingerprint_changes` 只能留空。

本模块把结论拆成四项独立输出，迁移前后各生成一次：

1. **原始字段投影摘要**（projection）：只对迁移前已存在的列做全量摘要，迁移前后必须相等；
2. **完整字段摘要**（full）：对迁移后全部列做全量摘要，用于记录「新增列带来了什么」；
3. **主键与记录数校验**（pk / rows）：按稳定主键顺序摘要，检测删除、重复与主键改写；
4. **Schema 变化清单**：表 / 列 / 索引 / 触发器的新增、删除与属性变化。

序列化规范化（保证跨进程、跨行序、跨 SQLite 版本稳定）：

- NULL 用固定哨兵 `<NULL>`，与空串、0 严格区分；
- bool → `0` / `1`；整数与整值浮点统一为十进制字符串（`1` 与 `1.0` 等价）；
- 其他浮点保留 12 位有效数字（价格量级足够，且避免二进制尾差）；
- 日期 / 日期时间文本（含 SQLAlchemy 的 `2026-11-09 12:00:00.000000`）归一为
  `YYYY-MM-DD` 或 `YYYY-MM-DDTHH:MM:SS[.f]`；
- 每个字段用「长度前缀 + UTF-8」编码，避免分隔符出现在值里造成碰撞；
- 行顺序由主键（无主键用 rowid）升序固定，摘要与插入顺序无关。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

NULL_TOKEN = "<NULL>"

_DATE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?)?$"
)


def _normalize_datetime_text(text: str) -> str:
    """把 ISO 日期 / 日期时间文本归一（不换算时区，避免伪造等价）。"""
    match = _DATE_RE.fullmatch(text)
    if not match:
        return text
    day, clock, fraction = match.groups()
    if clock is None:
        return day
    fraction = (fraction or "").rstrip("0")
    return f"{day}T{clock}" + (f".{fraction}" if fraction else "")


def normalize(value) -> str:
    """把单个字段值规范化为稳定字符串。"""
    if value is None:
        return NULL_TOKEN
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        if value == 0:
            return "0"
        if value.is_integer() and abs(value) < 1e16:
            return str(int(value))
        return format(value, ".12g")
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, (datetime, date)):
        return _normalize_datetime_text(value.isoformat(sep=" "))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "x'" + bytes(value).hex() + "'"
    return _normalize_datetime_text(str(value))


def _encode(value) -> bytes:
    raw = normalize(value).encode("utf-8")
    return str(len(raw)).encode("ascii") + b":" + raw


def connect_ro(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).resolve().as_posix()
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def table_meta(con: sqlite3.Connection, table: str) -> tuple[list[str], list[str]]:
    """返回（按 cid 排序列名, 按主键序号排序的主键列）。无主键时主键列为空。"""
    rows = con.execute(f'PRAGMA table_info("{table}")').fetchall()
    if not rows:
        raise KeyError(f"表不存在或没有列：{table}")
    columns = [row[1] for row in sorted(rows, key=lambda item: item[0])]
    primary_key = [
        row[1] for row in sorted((r for r in rows if r[5] > 0), key=lambda item: item[5])
    ]
    return columns, primary_key


def fingerprint_table(
    db_path: str | Path,
    table: str,
    columns: list[str] | None = None,
) -> dict:
    """对一张表做全量投影 / 完整摘要 + 主键与行数校验。

    `columns` 为空时使用表当前全部列（即「完整字段摘要」）；传迁移前的列清单
    即为「原始字段投影摘要」。
    """
    con = connect_ro(db_path)
    try:
        existing, primary_key = table_meta(con, table)
        used = list(columns) if columns is not None else list(existing)
        unknown = [name for name in used if name not in existing]
        if unknown:
            raise KeyError(f"{table} 缺少列：{unknown}")
        key = primary_key or ["rowid"]
        select_sql = ",".join(f'"{name}"' for name in key + used)
        order_sql = ",".join(f'"{name}"' for name in key)
        cursor = con.execute(f'SELECT {select_sql} FROM "{table}" ORDER BY {order_sql}')

        pk_digest = hashlib.sha256()
        content_digest = hashlib.sha256()
        rows = 0
        first_key = None
        last_key = None
        key_width = len(key)
        for row in cursor:
            row_key = row[:key_width]
            content = row[key_width:]
            pk_digest.update(b"".join(_encode(value) for value in row_key) + b"\n")
            content_digest.update(b"".join(_encode(value) for value in content) + b"\n")
            if first_key is None:
                first_key = [normalize(value) for value in row_key]
            last_key = [normalize(value) for value in row_key]
            rows += 1

        return {
            "table": table,
            "columns": used,
            "primary_key": primary_key,
            "rows": rows,
            "pk_sha256": pk_digest.hexdigest(),
            "content_sha256": content_digest.hexdigest(),
            "first_key": first_key,
            "last_key": last_key,
        }
    finally:
        con.close()


def compare_fingerprint(before: dict, after: dict) -> dict:
    """比较同一列投影的两次指纹；任何一项不等都视为数据变化。"""
    rows_match = before["rows"] == after["rows"]
    pk_match = before["pk_sha256"] == after["pk_sha256"]
    content_match = before["content_sha256"] == after["content_sha256"]
    return {
        "table": before["table"],
        "columns": before["columns"],
        "rows_before": before["rows"],
        "rows_after": after["rows"],
        "rows_match": rows_match,
        "pk_sha256_match": pk_match,
        "content_sha256_match": content_match,
        "unchanged": rows_match and pk_match and content_match,
        "before": before,
        "after": after,
    }


def schema_snapshot(db_path: str | Path) -> dict:
    """导出结构快照：表 / 列属性 / 索引（区分显式、UNIQUE 约束、主键）/ 触发器。"""
    con = connect_ro(db_path)
    try:
        tables: dict[str, dict] = {}
        names = [
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for name in names:
            columns = [
                {
                    "cid": row[0],
                    "name": row[1],
                    "type": row[2],
                    "notnull": bool(row[3]),
                    "default": row[4],
                    "pk": row[5],
                }
                for row in con.execute(f'PRAGMA table_info("{name}")')
            ]
            indexes = []
            for row in con.execute(f'PRAGMA index_list("{name}")'):
                index_name = row[1]
                indexes.append(
                    {
                        "name": index_name,
                        "unique": bool(row[2]),
                        "origin": row[3],
                        "partial": bool(row[4]),
                        "columns": [
                            info[2]
                            for info in con.execute(f'PRAGMA index_info("{index_name}")')
                        ],
                        "sql": (
                            con.execute(
                                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                                (index_name,),
                            ).fetchone()
                            or [None]
                        )[0],
                    }
                )
            tables[name] = {
                "columns": columns,
                "indexes": indexes,
                "sql": (
                    con.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                        (name,),
                    ).fetchone()
                    or [None]
                )[0],
            }

        triggers = [
            {"name": row[0], "table": row[1], "sql": row[2]}
            for row in con.execute(
                "SELECT name, tbl_name, sql FROM sqlite_master "
                "WHERE type='trigger' ORDER BY name"
            )
        ]
        origin_counts = {"c": 0, "u": 0, "pk": 0, "other": 0}
        for table in tables.values():
            for index in table["indexes"]:
                origin_counts[index["origin"]] = (
                    origin_counts.get(index["origin"], 0) + 1
                )
        payload = {"tables": tables, "triggers": triggers}
        serialized = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return {
            "table_count": len(tables),
            "trigger_count": len(triggers),
            "index_counts": {
                "explicit": origin_counts.get("c", 0),
                "unique_constraint": origin_counts.get("u", 0),
                "primary_key": origin_counts.get("pk", 0),
                "other": origin_counts.get("other", 0),
                "total": sum(len(table["indexes"]) for table in tables.values()),
            },
            "schema_sha256": hashlib.sha256(
                serialized.encode("utf-8")
            ).hexdigest(),
            "tables": tables,
            "triggers": triggers,
        }
    finally:
        con.close()


def _column_map(snapshot: dict, table: str) -> dict[str, dict]:
    return {column["name"]: column for column in snapshot["tables"][table]["columns"]}


def schema_diff(before: dict, after: dict) -> dict:
    """输出可读的 Schema 变化清单（迁移前 → 迁移后）。"""
    before_tables = set(before["tables"])
    after_tables = set(after["tables"])
    columns_added: dict[str, list[str]] = {}
    columns_removed: dict[str, list[str]] = {}
    columns_changed: dict[str, list[dict]] = {}
    indexes_added: list[str] = []
    indexes_removed: list[str] = []

    for table in sorted(before_tables & after_tables):
        old = _column_map(before, table)
        new = _column_map(after, table)
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        if added:
            columns_added[table] = added
        if removed:
            columns_removed[table] = removed
        changed = []
        for name in sorted(set(old) & set(new)):
            fields = ("type", "notnull", "default", "pk")
            delta = {
                field: {"before": old[name][field], "after": new[name][field]}
                for field in fields
                if old[name][field] != new[name][field]
            }
            if delta:
                changed.append({"column": name, "changes": delta})
        if changed:
            columns_changed[table] = changed

        old_indexes = {index["name"] for index in before["tables"][table]["indexes"]}
        new_indexes = {index["name"] for index in after["tables"][table]["indexes"]}
        indexes_added.extend(sorted(new_indexes - old_indexes))
        indexes_removed.extend(sorted(old_indexes - new_indexes))

    before_triggers = {trigger["name"] for trigger in before["triggers"]}
    after_triggers = {trigger["name"] for trigger in after["triggers"]}

    return {
        "tables_before": before["table_count"],
        "tables_after": after["table_count"],
        "tables_added": sorted(after_tables - before_tables),
        "tables_removed": sorted(before_tables - after_tables),
        "columns_added": columns_added,
        "columns_removed": columns_removed,
        "columns_changed": columns_changed,
        "indexes_added": indexes_added,
        "indexes_removed": indexes_removed,
        "triggers_before": before["trigger_count"],
        "triggers_after": after["trigger_count"],
        "triggers_added": sorted(after_triggers - before_triggers),
        "triggers_removed": sorted(before_triggers - after_triggers),
        "schema_sha256_before": before["schema_sha256"],
        "schema_sha256_after": after["schema_sha256"],
    }
