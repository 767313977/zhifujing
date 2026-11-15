"""M6.6 增补：导出请求预算账本与哈希复核。

只读账本与数据库；输出：
- reports/trend_engine/m66/ifind_personal_100/04-request-budget-ledger.csv
- reports/trend_engine/m66/ifind_personal_100/sha_check.json

用法：

    python scripts/m66_export_ledger.py --batch ifind_personal_100_xxx
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGING_ROOT = ROOT / "backend/data/m66_staging"
REPORT_DIR = ROOT / "reports/trend_engine/m66/ifind_personal_100"
M66_BASELINE = ROOT / "reports/trend_engine/m66/baseline_freeze.json"
M64_STAGING = ROOT / "backend/data/m64_staging/batch_20261115T152701Z"

COLUMNS = [
    "batch_id",
    "request_id",
    "sequence_no",
    "timestamp_utc",
    "tool_name",
    "endpoint",
    "purpose",
    "sample_ids",
    "request_hash",
    "response_status",
    "provider_error_code",
    "elapsed_ms",
    "returned_rows",
    "returned_cells",
    "retry_of",
    "budget_reserved",
    "payload_hash",
    "storage_status",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", required=True)
    args = parser.parse_args()

    batch = STAGING_ROOT / args.batch
    ledger = batch / "request_budget_ledger.jsonl"
    if not ledger.exists():
        raise SystemExit(f"账本不存在：{ledger}")

    entries = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    reserves = {item["request_id"]: item for item in entries if item.get("type") == "reserve"}
    results = {item["request_id"]: item for item in entries if item.get("type") == "result"}

    rows = []
    for request_id, reserve in reserves.items():
        result = results.get(request_id, {})
        rows.append(
            {
                "batch_id": reserve.get("batch_id"),
                "request_id": request_id,
                "sequence_no": reserve.get("sequence_no"),
                "timestamp_utc": reserve.get("timestamp_utc"),
                "tool_name": reserve.get("tool_name"),
                "endpoint": reserve.get("endpoint"),
                "purpose": reserve.get("purpose"),
                "sample_ids": ";".join(reserve.get("sample_ids") or []),
                "request_hash": reserve.get("request_hash"),
                "response_status": result.get("response_status", "NO_RESULT"),
                "provider_error_code": result.get("provider_error_code", ""),
                "elapsed_ms": result.get("elapsed_ms", ""),
                "returned_rows": result.get("returned_rows", ""),
                "returned_cells": result.get("returned_cells", ""),
                "retry_of": result.get("retry_of", ""),
                "budget_reserved": True,
                "payload_hash": result.get("payload_hash", ""),
                "storage_status": result.get("storage_status", ""),
            }
        )
    rows.sort(key=lambda item: item["sequence_no"])

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    with (REPORT_DIR / "04-request-budget-ledger.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    baseline = json.loads(M66_BASELINE.read_text(encoding="utf-8"))
    sha_check = {"batch": args.batch, "protected_dbs": {}, "m64_staging": {}}
    for name, meta in baseline["protected_dbs"].items():
        current = sha256_file(ROOT / meta["path"])
        sha_check["protected_dbs"][name] = {
            "before": meta["sha256"],
            "after": current,
            "unchanged": current == meta["sha256"],
        }
    unchanged_files = 0
    for name, meta in baseline["m64_staging"].items():
        current = sha256_file(M64_STAGING / name)
        same = current == meta["sha256"]
        unchanged_files += same
        sha_check["m64_staging"][name] = {"unchanged": same}
    sha_check["m64_staging_summary"] = {"unchanged": unchanged_files, "total": len(baseline["m64_staging"])}
    sha_check["all_protected_dbs_unchanged"] = all(item["unchanged"] for item in sha_check["protected_dbs"].values())
    (REPORT_DIR / "sha_check.json").write_text(
        json.dumps(sha_check, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    statuses: dict[str, int] = {}
    for row in rows:
        statuses[row["response_status"]] = statuses.get(row["response_status"], 0) + 1
    print(json.dumps({"requests": len(rows), "statuses": statuses, "sha_ok": sha_check["all_protected_dbs_unchanged"], "m64": sha_check["m64_staging_summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
