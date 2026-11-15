"""M6.6 Batch 0：基线与并发检查冻结。

只读：记录 Git 状态、四个受保护库 SHA256、M6.4 staging、M6.5 核心文件哈希与网络调用入口清单。
不写任何原始研究库，不发起任何网络请求。

用法：

    python scripts/m66_freeze_baseline.py
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = ROOT / "reports/trend_engine/m66"
M64_STAGING = ROOT / "backend/data/m64_staging/batch_20261115T152701Z"
M65_DIR = ROOT / "reports/trend_engine/m65"

PROTECTED_DBS = {
    "b1_research": ROOT / "backend/data/b1_research.db",
    "history": ROOT / "backend/data/history.db",
    "b2_final_snapshot": ROOT / "backend/data/b2_final_snapshot.db",
    "fupan": ROOT / "backend/data/fupan.db",
}

M65_FILES = (
    "backend/app/services/trend_engine/multisource.py",
    "scripts/m65_freeze_baseline.py",
    "scripts/m65_g1_preservation.py",
    "scripts/m65_write_schema_draft.py",
    "tests/test_trend_m65_guard.py",
    "tests/test_trend_m65_provider.py",
    "tests/test_trend_m65_staging.py",
)

NETWORK_PATTERNS = (
    "requests.",
    "httpx.",
    "urllib.request",
    "aiohttp",
    "urlopen(",
    "http://",
    "https://",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    return result.stdout.strip()


def scan_module(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    hits = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"') or stripped.startswith("'"):
            continue
        for pattern in NETWORK_PATTERNS:
            if pattern in line:
                hits.append({"line": lineno, "pattern": pattern, "text": stripped[:120]})
                break
    return {
        "file": str(path.relative_to(ROOT)),
        "sha256": sha256_file(path),
        "lines": len(text.splitlines()),
        "network_like_lines": hits,
        "has_sqlite3_connect": "sqlite3.connect" in text,
        "mentions_protected_db_names": [
            name for name in PROTECTED_DBS if f"{name}.db" in text
        ],
        "mentions_write_sql": [
            keyword
            for keyword in ("INSERT INTO index_daily", "DELETE FROM", "UPDATE ", "VACUUM")
            if re.search(rf"\b{re.escape(keyword)}", text)
        ],
    }


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git": {
            "root": str(ROOT),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "head": git("rev-parse", "HEAD"),
            "upstream": git("rev-parse", "--abbrev-ref", "HEAD@{upstream}"),
            "upstream_head": git("rev-parse", "HEAD@{upstream}"),
            "status_short": git("status", "--short").splitlines(),
            "log8": git("log", "-8", "--oneline", "--decorate").splitlines(),
            "worktrees": git("worktree", "list").splitlines(),
        },
        "protected_dbs": {},
        "m64_staging": {},
        "m65_core_files": {},
        "audit": {},
    }

    for name, path in PROTECTED_DBS.items():
        payload["protected_dbs"][name] = {
            "path": str(path.relative_to(ROOT)),
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }

    for path in sorted(M64_STAGING.iterdir()):
        if path.is_file():
            payload["m64_staging"][path.name] = {
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }

    for rel in M65_FILES:
        path = ROOT / rel
        if path.exists():
            payload["m65_core_files"][rel] = {
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }

    for path in sorted(M65_DIR.glob("*")):
        if path.is_file():
            payload["m65_core_files"][str(path.relative_to(ROOT))] = {
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }

    payload["audit"]["multisource"] = scan_module(ROOT / "backend/app/services/trend_engine/multisource.py")
    payload["audit"]["m65_scripts"] = [
        scan_module(path) for path in sorted((ROOT / "scripts").glob("m65_*.py"))
    ]
    payload["audit"]["network_entrypoints"] = {
        "multisource_module": "无网络调用（纯内存 + 本地 SQLite）",
        "existing_ifind_client": "backend/app/sources/ifind.py（M6.4 既有，本轮不调用）",
        "m66_scripts": "本轮新增脚本均为只读/离线，不发起网络请求",
    }

    out = REPORT_DIR / "baseline_freeze.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "branch": payload["git"]["branch"],
        "head": payload["git"]["head"],
        "upstream_head": payload["git"]["upstream_head"],
        "head_matches_upstream": payload["git"]["head"] == payload["git"]["upstream_head"],
        "status_short": payload["git"]["status_short"],
        "db_sha": {k: v["sha256"] for k, v in payload["protected_dbs"].items()},
        "m64_staging_files": len(payload["m64_staging"]),
        "m65_core_files": len(payload["m65_core_files"]),
        "multisource_network_lines": len(payload["audit"]["multisource"]["network_like_lines"]),
        "output": str(out.relative_to(ROOT)),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
