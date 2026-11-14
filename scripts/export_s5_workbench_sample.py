"""导出 S5 工作台的真实历史只读样例（不写库、不外呼）。

```powershell
.venv/Scripts/python.exe scripts/export_s5_workbench_sample.py `
  --db backend/data/b1_research.db --date 2026-09-24 `
  --sectors 801572,801366,803024 --out reports/s5_workbench/sample_2026-09-24.json
```
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import tracemalloc
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services import post_market_workbench  # noqa: E402


def _uri(path: Path) -> str:
    resolved = path.resolve()
    if not resolved.exists():
        raise SystemExit(f"数据库不存在：{resolved}")
    return f"sqlite:///file:{resolved.as_posix()}?mode=ro&immutable=1&uri=true"


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        )
        return out.stdout.strip()
    except Exception:  # noqa: BLE001
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--sectors", required=True, help="逗号分隔板块代码")
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    raw = args.db.lower()
    if "mode=rw" in raw or "mode=rwc" in raw:
        raise SystemExit("拒绝写模式数据库；只接受只读副本")

    engine = create_engine(_uri(Path(args.db)), future=True)
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    sectors = [item.strip() for item in args.sectors.split(",") if item.strip()]
    tracemalloc.start()
    started = time.perf_counter()
    with Session(engine) as session:
        payload = post_market_workbench.build(
            session,
            as_of=args.date,
            sector_codes=sectors,
            mode="OR",
            market="all",
            limit=args.limit,
            offset=0,
        )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "database": Path(args.db).name,
        "mode": "ro&immutable=1",
        "as_of": args.date.isoformat(),
        "sectors": sectors,
        "market": "all",
        "combine": "OR",
        "sql_statements": len(statements),
        "elapsed_ms": elapsed_ms,
        "peak_memory_kb": round(peak / 1024, 1),
        "payload": payload,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(
        f"rows={payload['pagination']['total']} sql={len(statements)} "
        f"elapsed_ms={elapsed_ms} peak_kb={round(peak / 1024, 1)}"
    )


if __name__ == "__main__":
    main()
