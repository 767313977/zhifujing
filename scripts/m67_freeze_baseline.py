"""M6.7 N0：基线、并发与受保护对象哈希冻结。

只读：四个原始研究库 + M6.4 staging 16 个文件 + M6.6 核心证据。
不写数据库、不联网、不修改 staging。

用法：

    python scripts/m67_freeze_baseline.py --tag before
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = ROOT / "reports/trend_engine/m67"
M66_BASELINE = ROOT / "reports/trend_engine/m66/baseline_freeze.json"
M64_STAGING = ROOT / "backend/data/m64_staging/batch_20261115T152701Z"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", choices=("before", "after"), default="before")
    args = parser.parse_args()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    baseline = json.loads(M66_BASELINE.read_text(encoding="utf-8"))

    payload = {
        "tag": args.tag,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git": {
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "head": git("rev-parse", "HEAD"),
            "status_short": git("status", "--short").splitlines(),
        },
        "protected_dbs": {},
        "m64_staging": {},
        "m66_core_files": {},
    }

    for name, meta in baseline["protected_dbs"].items():
        path = ROOT / meta["path"]
        current = sha256_file(path)
        payload["protected_dbs"][name] = {
            "path": meta["path"],
            "sha256_expected": meta["sha256"],
            "sha256_now": current,
            "unchanged": current == meta["sha256"],
        }

    for name, meta in baseline["m64_staging"].items():
        current = sha256_file(M64_STAGING / name)
        payload["m64_staging"][name] = {"unchanged": current == meta["sha256"], "sha256": current}

    for rel, meta in baseline.get("m65_core_files", {}).items():
        path = ROOT / rel
        if path.exists():
            payload["m66_core_files"][rel] = {"unchanged": sha256_file(path) == meta["sha256"]}

    payload["summary"] = {
        "dbs_unchanged": all(item["unchanged"] for item in payload["protected_dbs"].values()),
        "m64_unchanged": sum(1 for item in payload["m64_staging"].values() if item["unchanged"]),
        "m64_total": len(payload["m64_staging"]),
    }

    name = f"00-protected-hashes-{args.tag}.json" if args.tag == "before" else "08-m67-protected-hash-after.json"
    (REPORT_DIR / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.tag == "after":
        (REPORT_DIR / "07-m67-protected-hash-after.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps({"tag": args.tag, "head": payload["git"]["head"], **payload["summary"], "file": name}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
