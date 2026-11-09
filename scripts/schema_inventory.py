"""SQLite Schema 清点与差集（P1.0-01）。

只读打开（`mode=ro`），输出表 / 列（类型、非空、默认值、主键）/ 唯一约束 /
索引 / 触发器 / 视图的完整清单，并可与另一个库做双向差集。

用法：
    python scripts/schema_inventory.py --db backend/data/fupan.db
    python scripts/schema_inventory.py --db backend/data/fupan.db \
        --compare backend/data/p10_release_copy.db
    python scripts/schema_inventory.py --db ... --json backend/data/schema_a.json

⚠️ 不要用「已经跑过应用启动」的库代表原始基线：`init_db` 会自动补列，
那之后的库已经不是生产原样了。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def inventory(path: Path) -> dict:
    con = _connect(path)
    try:
        tables = [
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        out: dict = {
            "db": str(path),
            "size_bytes": path.stat().st_size,
            "tables": {},
            "indexes": {},
            "triggers": {},
            "views": {},
        }
        for table in tables:
            columns = [
                {
                    "name": row[1],
                    "type": (row[2] or "").upper(),
                    "notnull": bool(row[3]),
                    "default": row[4],
                    "pk": row[5],
                }
                for row in con.execute(f'PRAGMA table_info("{table}")')
            ]
            indexes = []
            for row in con.execute(f'PRAGMA index_list("{table}")'):
                name = row[1]
                columns_of_index = [
                    item[2] for item in con.execute(f'PRAGMA index_info("{name}")')
                ]
                indexes.append(
                    {
                        "name": name,
                        "unique": bool(row[2]),
                        "origin": row[3],
                        "columns": columns_of_index,
                    }
                )
                out["indexes"][name] = {"table": table, "unique": bool(row[2]), "columns": columns_of_index}
            out["tables"][table] = {
                "columns": columns,
                "indexes": sorted(index["name"] for index in indexes),
                "foreign_keys": [
                    [row[2], row[3], row[4]]  # 指向表、指向列、on delete
                    for row in con.execute(f'PRAGMA foreign_key_list("{table}")')
                ],
            }
        for row in con.execute("SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger'"):
            out["triggers"][row[0]] = {"table": row[1], "sql": row[2]}
        for row in con.execute("SELECT name, sql FROM sqlite_master WHERE type='view'"):
            out["views"][row[0]] = row[1]
        return out
    finally:
        con.close()


def _column_signature(column: dict) -> tuple:
    return (column["type"], column["notnull"], column["default"], column["pk"])


def compare(left: dict, right: dict) -> dict:
    lt, rt = set(left["tables"]), set(right["tables"])
    diff: dict = {
        "tables_only_in_left": sorted(lt - rt),
        "tables_only_in_right": sorted(rt - lt),
        "columns": {},
        "column_definition": {},
        "indexes_only_in_left": sorted(set(left["indexes"]) - set(right["indexes"])),
        "indexes_only_in_right": sorted(set(right["indexes"]) - set(left["indexes"])),
        "triggers_only_in_left": sorted(set(left["triggers"]) - set(right["triggers"])),
        "triggers_only_in_right": sorted(set(right["triggers"]) - set(left["triggers"])),
        "views_only_in_left": sorted(set(left["views"]) - set(right["views"])),
        "views_only_in_right": sorted(set(right["views"]) - set(left["views"])),
    }
    for table in sorted(lt & rt):
        lcols = {c["name"]: c for c in left["tables"][table]["columns"]}
        rcols = {c["name"]: c for c in right["tables"][table]["columns"]}
        only_left = sorted(set(lcols) - set(rcols))
        only_right = sorted(set(rcols) - set(lcols))
        if only_left or only_right:
            diff["columns"][table] = {"only_in_left": only_left, "only_in_right": only_right}
        changed = {
            name: {"left": lcols[name], "right": rcols[name]}
            for name in sorted(set(lcols) & set(rcols))
            if _column_signature(lcols[name]) != _column_signature(rcols[name])
        }
        if changed:
            diff["column_definition"][table] = changed
    return diff


def _print_inventory(data: dict) -> None:
    print(f"库：{data['db']}（{data['size_bytes']:,} 字节）")
    print(f"表 {len(data['tables'])} · 索引 {len(data['indexes'])} · 触发器 {len(data['triggers'])} · 视图 {len(data['views'])}")
    for table, info in data["tables"].items():
        print(f"  - {table}: {len(info['columns'])} 列 / {len(info['indexes'])} 索引")


def _print_diff(diff: dict) -> None:
    print(
        "差集："
        f"仅左侧表 {len(diff['tables_only_in_left'])}、仅右侧表 {len(diff['tables_only_in_right'])}、"
        f"列差异 {len(diff['columns'])} 张表、定义差异 {len(diff['column_definition'])} 张表、"
        f"索引 -{len(diff['indexes_only_in_left'])} +{len(diff['indexes_only_in_right'])}、"
        f"触发器 -{len(diff['triggers_only_in_left'])} +{len(diff['triggers_only_in_right'])}"
    )
    for table in diff["tables_only_in_left"]:
        print(f"  仅左侧表：{table}")
    for table in diff["tables_only_in_right"]:
        print(f"  仅右侧表：{table}")
    for table, cols in diff["columns"].items():
        if cols["only_in_left"]:
            print(f"  仅左侧列 {table}: {'、'.join(cols['only_in_left'])}")
        if cols["only_in_right"]:
            print(f"  仅右侧列 {table}: {'、'.join(cols['only_in_right'])}")
    for table, cols in diff["column_definition"].items():
        print(f"  定义变化 {table}: {'、'.join(cols)}")
    for name in diff["indexes_only_in_left"]:
        print(f"  仅左侧索引：{name}")
    for name in diff["indexes_only_in_right"]:
        print(f"  仅右侧索引：{name}")
    for name in diff["triggers_only_in_left"]:
        print(f"  仅左侧触发器：{name}")
    for name in diff["triggers_only_in_right"]:
        print(f"  仅右侧触发器：{name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite Schema 清点与差集（只读）")
    parser.add_argument("--db", required=True, help="要清点的库（只读打开）")
    parser.add_argument("--compare", help="与这个库做双向差集（左=--db，右=--compare）")
    parser.add_argument("--json", help="把完整清单写成 JSON")
    parser.add_argument("--diff-json", help="把差集写成 JSON")
    args = parser.parse_args()

    left = inventory(Path(args.db))
    _print_inventory(left)
    if args.json:
        Path(args.json).write_text(json.dumps(left, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.compare:
        right = inventory(Path(args.compare))
        print()
        print(f"对照：{right['db']}（{right['size_bytes']:,} 字节）")
        _print_inventory(right)
        print()
        diff = compare(left, right)
        _print_diff(diff)
        if args.diff_json:
            Path(args.diff_json).write_text(
                json.dumps(diff, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return 0 if not any(diff.values()) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
