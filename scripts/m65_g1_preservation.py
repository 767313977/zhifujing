"""M6.5 Batch 6：G1 指数 staging 保全与只读复核。

只读：不生成 DDL/DML，不合并，不写任何原始研究库。
输出：
- reports/trend_engine/m65/g1_staging_manifest.json
- reports/trend_engine/m65/g1_overlap_readonly_check.csv
- reports/trend_engine/m65/g1_history_coverage.csv

用法：

    python scripts/m65_g1_preservation.py
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = ROOT / "reports/trend_engine/m65"
M64_REPORT_DIR = ROOT / "reports/trend_engine/m64"
M64_STAGING = ROOT / "backend/data/m64_staging/batch_20261115T152701Z"
B1_DB = ROOT / "backend/data/b1_research.db"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def read_only(db: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def load_staging_rows() -> list[dict]:
    path = M64_REPORT_DIR / "index_daily_staging.csv"
    with path.open(encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def as_float(value):
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    return float(text)


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    baseline = json.loads((REPORT_DIR / "baseline_freeze.json").read_text(encoding="utf-8"))
    frozen = baseline["m64_staging"]["batch_20261115T152701Z"]

    # 1) 保全核对：本批次内文件是否与 M6.5 起始冻结值一致
    preserved = {}
    for name, meta in frozen.items():
        path = M64_STAGING / name
        current = sha256_file(path) if path.exists() else None
        preserved[name] = {
            "frozen_sha256": meta["sha256"],
            "current_sha256": current,
            "unchanged": current == meta["sha256"],
        }

    # 2) CSV 结构核对
    rows = load_staging_rows()
    codes = sorted({row["code"] for row in rows})
    keys = {(row["code"], row["trade_date"]) for row in rows}
    valid_rows = [row for row in rows if as_float(row.get("close")) is not None and as_float(row.get("open")) is not None]
    artifact_rows = [row for row in rows if as_float(row.get("open")) is None]
    valid_dates = sorted({row["trade_date"] for row in valid_rows})

    # 3) staging SQLite 与 CSV 一致性（只读）
    staging_db = M64_STAGING / "m64_ifind_staging.db"
    db_rows = []
    if staging_db.exists():
        connection = read_only(staging_db)
        db_rows = connection.execute(
            "SELECT code, trade_date, close, open FROM index_daily ORDER BY code, trade_date"
        ).fetchall()
        connection.close()
    db_keys = {(row[0], row[1]) for row in db_rows}
    csv_keys = keys

    # 4) 与 b1 重叠日期逐行比较（只读）
    b1_before = sha256_file(B1_DB)
    b1 = read_only(B1_DB)
    b1_map = {
        (code, trade_date): (close, pct_chg)
        for code, trade_date, close, pct_chg in b1.execute(
            "SELECT code, trade_date, close, pct_chg FROM index_daily"
        )
    }
    coverage = {
        code: b1.execute(
            "SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM index_daily WHERE code=?",
            (code,),
        ).fetchone()
        for code in codes
    }
    b1.close()
    b1_after = sha256_file(B1_DB)

    overlap_rows = []
    for row in valid_rows:
        key = (row["code"], row["trade_date"])
        if key not in b1_map:
            continue
        b1_close, b1_pct = b1_map[key]
        close = as_float(row["close"])
        pct = as_float(row.get("pct_chg"))
        abs_diff = abs(close - float(b1_close))
        rel_bp = (abs_diff / float(b1_close) * 10000) if b1_close else None
        overlap_rows.append(
            {
                "code": row["code"],
                "trade_date": row["trade_date"],
                "staging_close": close,
                "b1_close": b1_close,
                "abs_diff": round(abs_diff, 6),
                "rel_diff_bp": round(rel_bp, 4) if rel_bp is not None else "",
                "staging_pct_chg": "" if pct is None else pct,
                "b1_pct_chg": b1_pct,
                "pct_diff": "" if pct is None else round(pct - float(b1_pct), 6),
                "match_1bp": rel_bp is not None and rel_bp <= 1.0,
            }
        )

    with (REPORT_DIR / "g1_overlap_readonly_check.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(overlap_rows[0]) if overlap_rows else ["code"])
        writer.writeheader()
        writer.writerows(overlap_rows)

    coverage_rows = []
    for code in codes:
        b1_start, b1_end, b1_count = coverage[code]
        stage_valid = sorted({row["trade_date"] for row in valid_rows if row["code"] == code})
        gap = ""
        if b1_end and stage_valid:
            overlap_days = len([d for d in stage_valid if b1_start <= d <= b1_end])
        else:
            overlap_days = 0
        coverage_rows.append(
            {
                "code": code,
                "b1_start": b1_start,
                "b1_end": b1_end,
                "b1_rows": b1_count,
                "staging_valid_start": stage_valid[0] if stage_valid else "",
                "staging_valid_end": stage_valid[-1] if stage_valid else "",
                "staging_valid_days": len(stage_valid),
                "overlap_days": overlap_days,
                "post_b1_extension_days": len([d for d in stage_valid if b1_end and d > b1_end]),
                "gap_between": gap,
                "note": "staging 仅在原始研究库之外补齐，未合并",
            }
        )
    with (REPORT_DIR / "g1_history_coverage.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(coverage_rows[0]))
        writer.writeheader()
        writer.writerows(coverage_rows)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "batch": "m65-batch6-g1-preservation",
        "source_staging_batch": M64_STAGING.name,
        "preservation": preserved,
        "staging_all_unchanged": all(item["unchanged"] for item in preserved.values()),
        "csv": {
            "file": "reports/trend_engine/m64/index_daily_staging.csv",
            "sha256": sha256_file(M64_REPORT_DIR / "index_daily_staging.csv"),
            "rows": len(rows),
            "unique_code_date": len(keys),
            "codes": codes,
            "valid_rows_full_ohlc": len(valid_rows),
            "artifact_rows_halted_close": len(artifact_rows),
            "valid_trade_dates": valid_dates,
        },
        "staging_db": {
            "file": str(staging_db.relative_to(ROOT)),
            "rows": len(db_rows),
            "matches_csv_keys": db_keys == csv_keys,
        },
        "b1_readonly": {
            "sha256_before": b1_before,
            "sha256_after": b1_after,
            "unchanged": b1_before == b1_after,
            "overlap_rows_compared": len(overlap_rows),
            "overlap_matched_1bp": sum(1 for row in overlap_rows if row["match_1bp"]),
            "overlap_max_abs_diff": max((row["abs_diff"] for row in overlap_rows), default=0),
        },
        "guarantees": {
            "merge_performed": False,
            "ddl_executed": False,
            "dml_executed": False,
            "merge_script_generated": False,
            "approval_package_generated": False,
        },
    }
    (REPORT_DIR / "g1_staging_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "staging_unchanged": manifest["staging_all_unchanged"],
        "csv_rows": len(rows),
        "valid_rows": len(valid_rows),
        "artifact_rows": len(artifact_rows),
        "db_rows": len(db_rows),
        "overlap_rows": len(overlap_rows),
        "overlap_max_abs_diff": manifest["b1_readonly"]["overlap_max_abs_diff"],
        "b1_unchanged": manifest["b1_readonly"]["unchanged"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
