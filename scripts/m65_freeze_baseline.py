"""M6.5 Batch 0：基线与 M6.4 证据冻结。

只读：计算 M6.4 报告/staging 文件哈希与四个原始库 SHA256，写 `reports/trend_engine/m65/baseline_freeze.json`。
不修改任何 M6.4 报告，不写任何原始库。

用法：

    python scripts/m65_freeze_baseline.py
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = ROOT / "reports/trend_engine/m65"
M64_REPORT_DIR = ROOT / "reports/trend_engine/m64"
M64_STAGING_ROOT = ROOT / "backend/data/m64_staging"

PROTECTED_DBS = {
    "b1_research": ROOT / "backend/data/b1_research.db",
    "history": ROOT / "backend/data/history.db",
    "b2_final_snapshot": ROOT / "backend/data/b2_final_snapshot.db",
    "fupan": ROOT / "backend/data/fupan.db",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    staging_batches = sorted(M64_STAGING_ROOT.glob("batch_*")) if M64_STAGING_ROOT.exists() else []
    if not staging_batches:
        raise SystemExit("M6.4 staging 批次不存在，无法冻结")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git": {
            "head": git("rev-parse", "HEAD"),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "remote_head": git("rev-parse", "origin/feat/trend-engine-m64-ifind-data-ingest"),
            "upstream": git("rev-parse", "--abbrev-ref", "HEAD@{upstream}"),
            "status_porcelain": git("status", "--porcelain=v1").splitlines(),
        },
        "m64_reports": {},
        "m64_staging": {},
        "protected_dbs": {},
    }

    for path in sorted(M64_REPORT_DIR.iterdir()):
        if path.is_file():
            payload["m64_reports"][path.name] = {
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }

    for batch in staging_batches:
        files = {}
        for path in sorted(batch.iterdir()):
            if path.is_file():
                files[path.name] = {
                    "sha256": sha256_file(path),
                    "bytes": path.stat().st_size,
                }
        payload["m64_staging"][batch.name] = files

    for name, path in PROTECTED_DBS.items():
        if not path.exists():
            payload["protected_dbs"][name] = {"path": str(path), "sha256": None, "exists": False}
            continue
        payload["protected_dbs"][name] = {
            "path": str(path.relative_to(ROOT)),
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
            "exists": True,
        }

    payload["protected_dbs_unchanged_snapshot"] = all(
        item.get("exists") for item in payload["protected_dbs"].values()
    )

    out = REPORT_DIR / "baseline_freeze.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "head": payload["git"]["head"],
        "branch": payload["git"]["branch"],
        "m64_report_files": len(payload["m64_reports"]),
        "m64_staging_batches": list(payload["m64_staging"]),
        "db_sha": {k: v["sha256"] for k, v in payload["protected_dbs"].items()},
        "output": str(out.relative_to(ROOT)),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
