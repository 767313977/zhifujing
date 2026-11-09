"""生成发布候选清单（Release Candidate Manifest，P1.1-01）。

输出一个可存档的 JSON：完整 commit SHA / tree SHA、分支、依赖版本、
Schema 指纹、启动模式、构建产物摘要、数据导入来源摘要。

用法：
    python scripts/release_manifest.py --out docs/p1.1/rc-manifest.json
    python scripts/release_manifest.py --hash-data      # 额外对研究库做 sha256
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

PRODUCTION_BASE = "44f0912"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _installed_versions() -> dict:
    import fastapi
    import sqlalchemy
    import uvicorn

    return {
        "python": sys.version.split()[0],
        "fastapi": fastapi.__version__,
        "sqlalchemy": sqlalchemy.__version__,
        "uvicorn": uvicorn.__version__,
    }


def schema_fingerprint() -> tuple[str, int, int, int]:
    """在一张全新的临时库上跑显式迁移，对完整 Schema 做稳定摘要。"""
    from app import db
    from sqlalchemy import create_engine, inspect

    with tempfile.TemporaryDirectory() as tmp:
        engine = create_engine(f"sqlite:///{Path(tmp) / 'schema.db'}", future=True)
        db.migrate_schema(engine)
        inspector = inspect(engine)
        lines: list[str] = []
        for table in sorted(inspector.get_table_names()):
            for column in inspector.get_columns(table):
                lines.append(
                    f"{table}|{column['name']}|{column['type']}|{column['nullable']}|{column['default']}"
                )
            for index in inspector.get_indexes(table):
                lines.append(f"idx|{table}|{index['name']}|{index['unique']}|{index['column_names']}")
        engine.dispose()
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    tables = len({line.split("|", 1)[0] for line in lines if not line.startswith("idx|")})
    columns = len([line for line in lines if not line.startswith("idx|")])
    indexes = len([line for line in lines if line.startswith("idx|")])
    return digest, tables, columns, indexes


def dist_digest() -> dict:
    dist = ROOT / "frontend" / "dist"
    if not dist.exists():
        return {"exists": False}
    files = []
    for path in sorted(p for p in dist.rglob("*") if p.is_file()):
        files.append(
            {
                "path": str(path.relative_to(dist)).replace("\\", "/"),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    total = hashlib.sha256(
        "\n".join(f"{item['path']}:{item['sha256']}" for item in files).encode("utf-8")
    ).hexdigest()
    return {"exists": True, "files": files, "count": len(files), "digest": total}


def data_source(path: Path, with_hash: bool) -> dict:
    if not path.exists():
        return {"path": str(path), "exists": False}
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        rps = con.execute(
            "SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM rps_daily"
        ).fetchone()
        member = con.execute(
            "SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM sector_member"
        ).fetchone()
        manifest = (
            con.execute("SELECT COUNT(*) FROM sector_member_backfill").fetchone()[0]
            if con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sector_member_backfill'"
            ).fetchone()
            else None
        )
    finally:
        con.close()
    payload = {
        "path": str(path),
        "exists": True,
        "size_bytes": path.stat().st_size,
        "rps": {"rows": rps[0], "min": rps[1], "max": rps[2]},
        "sector_member": {"rows": member[0], "min": member[1], "max": member[2]},
        "manifest_rows": manifest,
    }
    if with_hash:
        payload["sha256"] = _sha256(path)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="生成发布候选清单")
    parser.add_argument("--out", required=True, help="输出 JSON 路径")
    parser.add_argument("--hash-data", action="store_true", help="对研究库做 sha256（较慢）")
    parser.add_argument("--tests", default="", help="本次测试结果摘要（人工填写）")
    args = parser.parse_args()

    fingerprint, tables, columns, indexes = schema_fingerprint()
    payload = {
        "generated_at": subprocess.run(
            ["git", "-C", str(ROOT), "log", "-1", "--format=%cI"], capture_output=True, text=True
        ).stdout.strip(),
        "git": {
            "head": _git("rev-parse", "HEAD"),
            "tree": _git("rev-parse", "HEAD^{tree}"),
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "subject": _git("log", "-1", "--format=%s"),
            "production_base": PRODUCTION_BASE,
            "commits_since_production": int(
                _git("rev-list", "--count", f"{PRODUCTION_BASE}..HEAD") or 0
            ),
            "merges_since_production": int(
                _git("rev-list", "--count", "--merges", f"{PRODUCTION_BASE}..HEAD") or 0
            ),
            "dirty": bool(_git("status", "--porcelain").strip()),
            "worktree_fingerprint": hashlib.sha256(
                _git("status", "--porcelain").encode("utf-8")
            ).hexdigest(),
        },
        "dependencies": _installed_versions(),
        "schema": {
            "fingerprint": fingerprint,
            "tables": tables,
            "columns": columns,
            "indexes": indexes,
            "startup_mode_default": os.environ.get("FUPAN_SCHEMA_MODE", "verify"),
            "migration_entry": "scripts/migrate_db.py --apply",
        },
        "tests": args.tests,
        "data_source": data_source(ROOT / "backend" / "data" / "b1_research.db", args.hash_data),
        "frontend_dist": dist_digest(),
        "node": {
            "node": subprocess.run(["node", "-v"], capture_output=True, text=True).stdout.strip(),
            "npm": subprocess.run(["npm", "-v"], capture_output=True, text=True).stdout.strip(),
        },
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written {out}")
    print(json.dumps({k: payload[k] for k in ("git", "schema")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
