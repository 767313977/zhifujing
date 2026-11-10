"""在隔离库（或显式授权的主库）安装研究缓存的数据修订水位（P1）。

默认拒绝主库 `backend/data/fupan.db`：该机制会新增表与触发器，属于结构变更，
必须先在隔离库验证。主库操作必须同时显式指定 `--db` 与 `--allow-main-db`。

用法：
    # 隔离库：先从主库在线备份，再安装
    python scripts/install_data_revision.py --copy-from backend/data/fupan.db \
        --db backend/data/revtest.db

    # 查看状态（只读，主库也可）
    python scripts/install_data_revision.py --db backend/data/b1_research_copy.db --status

    # 主库安装：必须显式加 --allow-main-db
    python scripts/install_data_revision.py --db backend/data/fupan.db --allow-main-db

安全边界：
- 默认拒绝主库；`--allow-main-db` 是唯一放行开关，与 `migrate_db.py` 一致；
- 安装前用 `db.schema_report` 校验 Schema 版本（缺表/缺列/缺索引直接拒绝）；
- 安装后核对触发器：**名称、绑定的表、INSERT/UPDATE/DELETE 事件、实际 SQL 定义**；
- 本地/隔离库（缺部分域表）按「现有表 × ai/au/ad」核对，并明确标注**不代表生产验收**；
- **生产验收（主库，或显式 `--strict`）必须 8 个数据域齐全、正好 24 条触发器**，
  不允许用「已存在域表 × 3」宣称完成；
- 幂等：`CREATE TRIGGER IF NOT EXISTS`，重复执行 db_uuid 不变、触发器数不变、不删数据；
- `--status` 只读打开，目标路径不存在时直接报错，**不会创建空库**。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import db  # noqa: E402
from app.services import data_revision  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

MAIN_DB = ROOT / "backend" / "data" / "fupan.db"

#: 后缀 → 触发器事件。`data_revision._SUFFIXES` 就是这三个。
_SUFFIX_ACTION = {"ai": "INSERT", "au": "UPDATE", "ad": "DELETE"}

#: 生产验收口径：8 个数据域 × 3 个事件。少一条都不算完成。
EXPECTED_PROD_TRIGGERS = len(data_revision.DOMAINS) * len(_SUFFIX_ACTION)


def _is_main_db(path: Path) -> bool:
    return os.path.normcase(str(path.resolve())) == os.path.normcase(str(MAIN_DB.resolve()))


def _copy_db(src: Path, dst: Path) -> None:
    if not src.exists():
        raise SystemExit(f"源库不存在：{src}")
    if dst.exists():
        raise SystemExit(f"目标已存在，拒绝覆盖：{dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(str(dst))
    try:
        source.backup(target)
        target.execute("PRAGMA journal_mode=WAL")
    finally:
        target.close()
        source.close()


def _verify_schema(engine) -> None:
    """安装前校验 Schema 版本：缺表/缺列/缺索引直接拒绝，不在半成品库上装触发器。"""
    report = db.schema_report(engine)
    if not report["ok"]:
        detail = []
        if report["missing_tables"]:
            detail.append("缺表 " + "、".join(report["missing_tables"][:8]))
        if report["missing_columns"]:
            detail.append("缺列 " + "、".join(report["missing_columns"][:8]))
        if report["missing_indexes"]:
            detail.append("缺索引 " + "、".join(report["missing_indexes"][:8]))
        raise SystemExit(
            f"Schema 校验未通过，拒绝安装触发器：{'；'.join(detail)}"
            f"\n请先执行 scripts/migrate_db.py --apply --db <path>"
        )


def _existing_tables(connection) -> set[str]:
    return {
        row[0]
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _require_all_domains(connection) -> None:
    """生产验收前置检查：8 个域表必须齐全，**在安装之前**失败（不留半装状态）。"""
    domain_tables = {
        table for tables_for in data_revision.DOMAINS.values() for table in tables_for
    }
    missing = sorted(domain_tables - _existing_tables(connection))
    if missing:
        raise SystemExit(
            f"严格校验失败：生产验收要求 {len(domain_tables)} 个数据域齐全，"
            f"当前缺 {len(missing)} 张域表：{'、'.join(missing)}"
            "\n不能按「已存在域表 × 3」宣称完成，请先补齐这些表再安装（本次未写入任何东西）。"
        )


def _normalize_sql(sql: str | None) -> str:
    """把触发器定义压成单行空白，便于逐项核对事件与正文。"""
    return " ".join((sql or "").split())


def _trigger_problems(connection, tables: set[str], *, strict: bool) -> tuple[dict, list[str]]:
    """核对触发器：名称、绑定表、事件、SQL 正文。返回 (期望清单, 问题列表)。"""
    domain_tables = {table for tables_for in data_revision.DOMAINS.values() for table in tables_for}
    missing_domains = sorted(domain_tables - tables)
    if strict and missing_domains:
        raise SystemExit(
            f"严格校验失败：生产验收要求 {len(domain_tables)} 个数据域齐全，"
            f"当前缺 {len(missing_domains)} 张域表：{'、'.join(missing_domains)}"
            "\n不能按「已存在域表 × 3」宣称完成，请先补齐这些表再安装。"
        )

    expected: dict[str, tuple[str, str, str]] = {}
    for domain, domain_tables_for in data_revision.DOMAINS.items():
        for table in domain_tables_for:
            if table not in tables:
                continue
            for suffix, action in _SUFFIX_ACTION.items():
                expected[data_revision.trigger_name(table, suffix)] = (table, action, domain)

    found = {
        name: (tbl_name, sql)
        for name, tbl_name, sql in connection.exec_driver_sql(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger'"
        ).all()
    }

    problems: list[str] = []
    for name, (table, action, domain) in sorted(expected.items()):
        if name not in found:
            problems.append(f"缺 {name}")
            continue
        tbl_name, sql = found[name]
        if tbl_name != table:
            problems.append(f"{name} 绑定了 {tbl_name}，应为 {table}")
            continue
        body = _normalize_sql(sql)
        if f"AFTER {action} ON {table}" not in body:
            problems.append(f"{name} 不是 AFTER {action} ON {table}")
        elif "INSERT INTO data_revision" not in body:
            problems.append(f"{name} 未写入 data_revision")
        elif "revision=revision+1" not in body.replace(" ", ""):
            problems.append(f"{name} 未递增 revision")
        elif f"'{domain}'" not in body:
            problems.append(f"{name} 的数据域不是 {domain}")
    return expected, problems


def _verify_triggers(session: Session, *, strict: bool = False) -> int:
    """安装后核对触发器，返回核对条数；不符抛 SystemExit。

    `strict=True`（生产验收）要求：8 个域表齐全、正好 24 条、逐条核对名称/绑定表/
    事件/SQL 正文；`strict=False`（本地或隔离库）允许缺表，按现有表 × ai/au/ad 核对，
    输出里必须标明**不代表生产验收**。
    """
    connection = session.connection()
    tables = {
        row[0]
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    expected, problems = _trigger_problems(connection, tables, strict=strict)
    if problems:
        raise SystemExit(f"触发器核对失败：{'；'.join(problems[:8])}")

    total = len(expected)
    if strict:
        if total != EXPECTED_PROD_TRIGGERS:
            raise SystemExit(
                f"严格校验失败：期望 {EXPECTED_PROD_TRIGGERS} 条触发器，实际核对 {total} 条"
            )
        if not data_revision.installed(session):
            raise SystemExit("严格校验失败：data_revision / db_uuid 不完整")
        print(
            f"触发器严格校验通过：{len(data_revision.DOMAINS)} 个数据域 / {total} 条"
            "（名称、绑定表、INSERT/UPDATE/DELETE 事件、SQL 定义均已核对）"
        )
    else:
        print(
            f"触发器核对通过：{total} 条（按现有表 × ai/au/ad，"
            f"生产验收口径应为 {EXPECTED_PROD_TRIGGERS} 条）—— 本地/隔离库结果，不代表生产验收"
        )
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="安装/查看研究缓存修订水位")
    parser.add_argument("--db", required=True, help="目标 SQLite（必须显式指定）")
    parser.add_argument("--copy-from", help="先用在线备份 API 从该库复制到 --db")
    parser.add_argument("--status", action="store_true", help="只查看当前修订水位，不安装")
    parser.add_argument("--uninstall", action="store_true", help="移除表与触发器（仅隔离库）")
    parser.add_argument(
        "--allow-main-db",
        action="store_true",
        help="允许对主库 backend/data/fupan.db 执行安装/卸载（默认拒绝）",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="生产验收口径：要求 8 个数据域齐全、正好 24 条触发器（主库自动开启）",
    )
    args = parser.parse_args()

    target = Path(args.db)
    is_main = _is_main_db(target)
    # 主库写操作（非 --status）必须显式 --allow-main-db
    if is_main and not args.status and not args.allow_main_db:
        raise SystemExit(
            "拒绝修改主库结构：请先复制到隔离库，或显式加 --allow-main-db"
        )
    # 生产目标（主库）不允许用「现有表 × 3」的宽容口径验收
    strict = args.strict or is_main
    if args.copy_from:
        _copy_db(Path(args.copy_from), target)
    if not target.exists():
        raise SystemExit(f"目标库不存在：{target}")

    if args.status:
        # 查看主库也只能只读打开：不能因为「看状态」而创建表/触发器
        engine = create_engine(
            f"sqlite:///file:{target.resolve().as_posix()}?mode=ro&uri=true", future=True
        )
    else:
        engine = create_engine(f"sqlite:///{target}", future=True)
    with Session(engine) as session:
        if args.status:
            if not data_revision.installed(session):
                print("未安装修订水位：研究缓存按 fail-closed 关闭（每次直接查库）")
                return 0
            rows = session.connection().exec_driver_sql(
                "SELECT domain, revision FROM data_revision ORDER BY domain"
            ).all()
            meta = session.connection().exec_driver_sql(
                "SELECT value FROM data_revision_meta WHERE key='db_uuid'"
            ).first()
            print(f"db_uuid={meta[0] if meta else ''}")
            for domain, revision in rows:
                print(f"{domain}: {revision}")
            return 0
        if args.uninstall:
            data_revision.uninstall(session)
            print("已移除数据修订水位")
            return 0
        # 安装前校验 Schema 版本；生产口径还要求 8 个域表齐全（不留半装状态）
        _verify_schema(engine)
        if strict:
            _require_all_domains(session.connection())
        db_uuid = data_revision.install(session)
        print(f"已安装：db_uuid={db_uuid}")
        # 安装后核对触发器（生产目标走严格口径：8 域 / 24 条）
        _verify_triggers(session, strict=strict)
        for domain, revision in session.connection().exec_driver_sql(
            "SELECT domain, revision FROM data_revision ORDER BY domain"
        ).all():
            print(f"{domain}: {revision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
