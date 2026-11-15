"""M6.3 Batch 4：RPS v2 版本化、五窗口核验与 sandbox 重算（只读原库）。

产物：
- `reports/trend_engine/m63/05-rps-v2-implementation.md`
- `reports/trend_engine/m63/rps_version_manifest.json`
- `reports/trend_engine/m63/rps_window_reconciliation.csv`
- `reports/trend_engine/m63/rps_coverage.csv`
- `backend/data/m63_sandbox/m63_derived.db`（新文件，创建前必须不存在）

用法：

    python scripts/m63_rps_v2_recompute.py
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

import sys

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.trend_engine import rps_versioning as versioning  # noqa: E402

DB = ROOT / "backend/data/b1_research.db"
OUT = ROOT / "reports/trend_engine/m63"
SANDBOX = ROOT / "backend/data/m63_sandbox/m63_derived.db"
VERSION_ID = "rps_v2_m63_20261115"
SAMPLE_DATES = ("2025-10-14", "2026-01-08", "2026-04-10", "2026-07-09", "2026-09-30")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=60.0)
    connection.execute("PRAGMA query_only=ON")
    return connection


def load_matrix(connection: sqlite3.Connection) -> tuple[list[str], list[str], np.ndarray]:
    dates = [row[0] for row in connection.execute("SELECT DISTINCT trade_date FROM stock_daily ORDER BY trade_date")]
    codes = [row[0] for row in connection.execute("SELECT DISTINCT code FROM stock_daily ORDER BY code")]
    date_pos = {day: index for index, day in enumerate(dates)}
    code_pos = {code: index for index, code in enumerate(codes)}
    matrix = np.full((len(codes), len(dates)), np.nan, dtype=float)
    for code, day, close in connection.execute(
        "SELECT code, trade_date, close FROM stock_daily WHERE close IS NOT NULL AND close > 0"
    ):
        matrix[code_pos[code], date_pos[day]] = float(close)
    return codes, dates, matrix


def _corr(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 10:
        return None
    left = np.asarray([a for a, _ in pairs])
    right = np.asarray([b for _, b in pairs])
    if np.std(left) == 0 or np.std(right) == 0:
        return None
    value = float(np.corrcoef(left, right)[0, 1])
    return value if np.isfinite(value) else None


def verify_windows(
    connection: sqlite3.Connection, codes: list[str], dates: list[str], matrix: np.ndarray
) -> list[dict]:
    date_pos = {day: index for index, day in enumerate(dates)}
    rows = []
    for day in SAMPLE_DATES:
        di = date_pos.get(day)
        if di is None:
            continue
        legacy_rows = connection.execute(
            "SELECT code, ret20, rps20, ret50, rps50, ret60, rps60, ret120, rps120, ret250, rps250 "
            "FROM rps_daily WHERE trade_date = ?",
            (day,),
        ).fetchall()
        legacy = {row[0]: row[1:] for row in legacy_rows}
        column_map = {20: (0, 1), 50: (2, 3), 60: (4, 5), 120: (6, 7), 250: (8, 9)}
        for window in versioning.WINDOWS:
            if di < window:
                rows.append({"trade_date": day, "window": window, "status": "INSUFFICIENT_HISTORY", "codes": 0})
                continue
            ret = matrix[:, di] / matrix[:, di - window] - 1
            valid = np.isfinite(ret)
            computed_codes = [codes[index] for index in np.flatnonzero(valid)]
            computed = {code: float(value * 100) for code, value in zip(computed_codes, ret[valid])}
            ranks = versioning.cross_section_ranks(computed)
            pairs_ret = []
            pairs_rps = []
            ret_index, rps_index = column_map[window]
            for code, value in computed.items():
                legacy_row = legacy.get(code)
                if not legacy_row:
                    continue
                ret_value = legacy_row[ret_index]
                rps_value = legacy_row[rps_index]
                if ret_value is not None:
                    pairs_ret.append((value, float(ret_value)))
                if rps_value is not None:
                    pairs_rps.append((ranks[code], float(rps_value)))
            ret_corr = _corr(pairs_ret)
            ret_diff = (
                float(np.median(np.abs(np.asarray([a for a, _ in pairs_ret]) - np.asarray([b for _, b in pairs_ret]))))
                if pairs_ret
                else None
            )
            rps_corr = _corr(pairs_rps)
            rps_diff = (
                float(np.mean(np.abs(np.asarray([a for a, _ in pairs_rps]) - np.asarray([b for _, b in pairs_rps]))))
                if pairs_rps
                else None
            )
            # 诊断：只用 legacy ret 值重排名，检验「百分位映射」是否与 legacy 一致
            legacy_ret_values = {
                code: float(legacy_row[ret_index])
                for code, legacy_row in legacy.items()
                if legacy_row[ret_index] is not None
            }
            legacy_ranks = versioning.cross_section_ranks(legacy_ret_values)
            mapping_pairs = [
                (legacy_ranks[code], float(legacy_row[rps_index]))
                for code, legacy_row in legacy.items()
                if legacy_row[rps_index] is not None and code in legacy_ranks
            ]
            mapping_corr = _corr(mapping_pairs)
            rows.append(
                {
                    "trade_date": day,
                    "window": window,
                    "status": "OK" if pairs_ret else "NO_LEGACY",
                    "codes": len(computed_codes),
                    "legacy_ret_pairs": len(pairs_ret),
                    "ret_corr": round(ret_corr, 4) if ret_corr is not None else None,
                    "ret_median_abs_diff_pp": round(ret_diff, 4) if ret_diff is not None else None,
                    "legacy_rps_pairs": len(pairs_rps),
                    "rps_corr": round(rps_corr, 4) if rps_corr is not None else None,
                    "rps_mean_abs_diff": round(rps_diff, 4) if rps_diff is not None else None,
                    "mapping_corr_on_legacy_ret": round(mapping_corr, 4) if mapping_corr is not None else None,
                }
            )
    return rows


def check_future_independence(codes: list[str], dates: list[str], matrix: np.ndarray, day: str) -> dict:
    di = dates.index(day)
    full = matrix[:, di] / matrix[:, di - 20] - 1
    truncated = matrix[:, : di + 1][:, di] / matrix[:, : di + 1][:, di - 20] - 1
    mask = np.isfinite(full)
    diff = np.abs(full[mask] - truncated[mask])
    return {
        "trade_date": day,
        "codes": int(mask.sum()),
        "max_abs_diff": float(np.nanmax(diff)) if diff.size else None,
        "identical": bool(diff.size == 0 or np.nanmax(diff) < 1e-12),
    }


def build_sandbox(connection: sqlite3.Connection) -> dict:
    if SANDBOX.exists():
        raise SystemExit(f"拒绝执行：目标 sandbox 已存在 {SANDBOX}")
    SANDBOX.parent.mkdir(parents=True, exist_ok=True)
    codes, dates, matrix = load_matrix(connection)
    date_pos = {day: index for index, day in enumerate(dates)}
    research_end = dates[-1]
    start_index = versioning.WINDOWS[-1]
    version = versioning.RpsVersion(
        version_id=VERSION_ID,
        algo="trailing_return_rank_percentile",
        windows=versioning.WINDOWS,
        universe_rule="ALL_CODES_WITH_RAW_CLOSE_AND_SUFFICIENT_HISTORY",
        st_rule="NOT_APPLIED_NO_HISTORY",
        new_listing_rule="REQUIRE_FULL_WINDOW",
        suspension_rule="TRADING_ROW_BASED",
        rank_method="average_rank_percentile_0_100",
        input_start=date.fromisoformat(dates[0]),
        input_cutoff=date.fromisoformat(research_end),
        db_sha256=sha256_file(DB),
        adjustment_method="UNADJUSTED_RAW",
        code_sha=sha256_file(Path(__file__)),
        created_at=versioning.version_stamp(VERSION_ID),
        quality_status="PARTIAL",
        notes=(
            "无复权因子：除权日收益会失真，版本状态 PARTIAL",
            "ST/退市不可识别，股票池未做 ST 剔除",
            "旧 rps_daily 不改动；本版本仅写 sandbox",
        ),
    )
    connection.close()

    sandbox = sqlite3.connect(SANDBOX)
    try:
        sandbox.execute("PRAGMA journal_mode=WAL")
        sandbox.execute(
            """
            CREATE TABLE rps_v2 (
              trade_date TEXT NOT NULL,
              code TEXT NOT NULL,
              version_id TEXT NOT NULL,
              ret20 REAL, ret50 REAL, ret60 REAL, ret120 REAL, ret250 REAL,
              rps20 REAL, rps50 REAL, rps60 REAL, rps120 REAL, rps250 REAL,
              PRIMARY KEY (trade_date, code, version_id)
            )
            """
        )
        sandbox.execute("CREATE TABLE rps_meta (version_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        sandbox.execute(
            "INSERT INTO rps_meta (version_id, payload) VALUES (?, ?)",
            (VERSION_ID, json.dumps(version.as_dict(), ensure_ascii=False)),
        )
        coverage_rows = []
        total_rows = 0
        for di in range(start_index, len(dates)):
            day = dates[di]
            batch = []
            counts = {window: 0 for window in versioning.WINDOWS}
            rps_counts = {window: 0 for window in versioning.WINDOWS}
            for window in versioning.WINDOWS:
                ret = matrix[:, di] / matrix[:, di - window] - 1
                valid = np.isfinite(ret)
                values = {codes[index]: float(ret[index] * 100) for index in np.flatnonzero(valid)}
                ranks = versioning.cross_section_ranks(values)
                counts[window] = len(values)
                rps_counts[window] = len(ranks)
                for code, value in values.items():
                    batch.append(
                        (
                            day,
                            code,
                            VERSION_ID,
                            *[
                                round(values[code], 6)
                                if other == window
                                else None
                                for other in versioning.WINDOWS
                            ],
                            *[
                                round(ranks[code], 4)
                                if other == window
                                else None
                                for other in versioning.WINDOWS
                            ],
                        )
                    )
            # 合并同一天同一 code 的五个窗口
            merged: dict[str, list] = {}
            for row in batch:
                key = row[1]
                current = merged.setdefault(key, [None] * 10)
                for index, value in enumerate(row[3:]):
                    if value is not None:
                        current[index] = value
            rows = [
                (day, code, VERSION_ID, *values)
                for code, values in merged.items()
            ]
            sandbox.executemany(
                "INSERT INTO rps_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
            )
            total_rows += len(rows)
            day_universe = int(np.isfinite(matrix[:, di]).sum())
            coverage_rows.append(
                {
                    "trade_date": day,
                    "universe": day_universe,
                    "ret20": counts[20],
                    "ret250": counts[250],
                    "rps20": rps_counts[20],
                    "rps250": rps_counts[250],
                    "coverage_ret20_pct": round(counts[20] / max(day_universe, 1) * 100, 2),
                    "status": "OK" if counts[20] >= 0.9 * max(day_universe, 1) else "PARTIAL",
                }
            )
        sandbox.commit()
        sandbox.execute("CREATE INDEX idx_rps_v2_code ON rps_v2 (code, trade_date)")
        sandbox.commit()
    finally:
        sandbox.close()
    return {
        "version": version.as_dict(),
        "rows": total_rows,
        "coverage": coverage_rows,
        "sandbox": str(SANDBOX),
        "sandbox_sha256": sha256_file(SANDBOX),
        "date_pos": date_pos,
        "research_end": research_end,
        "start_index": start_index,
    }


def load_existing_sandbox() -> dict:
    if not SANDBOX.exists():
        raise SystemExit(f"sandbox 不存在：{SANDBOX}")
    connection = sqlite3.connect(f"file:{SANDBOX.as_posix()}?mode=ro", uri=True)
    b1 = connect()
    try:
        stock_counts = {
            day: count
            for day, count in b1.execute(
                "SELECT trade_date, COUNT(*) FROM stock_daily WHERE close IS NOT NULL GROUP BY trade_date"
            )
        }
        payload = json.loads(
            connection.execute("SELECT payload FROM rps_meta WHERE version_id = ?", (VERSION_ID,)).fetchone()[0]
        )
        rows = connection.execute("SELECT COUNT(*) FROM rps_v2 WHERE version_id = ?", (VERSION_ID,)).fetchone()[0]
        coverage_rows = [
            {
                "trade_date": day,
                "universe": stock_counts.get(day, universe),
                "ret20": ret20,
                "ret250": ret250,
                "rps20": ret20,
                "rps250": ret250,
                "coverage_ret20_pct": round(ret20 / max(stock_counts.get(day, universe), 1) * 100, 2),
                "status": "OK" if ret20 >= 0.9 * max(stock_counts.get(day, universe), 1) else "PARTIAL",
            }
            for day, universe, ret20, ret250 in connection.execute(
                "SELECT trade_date, COUNT(*), SUM(ret20 IS NOT NULL), SUM(ret250 IS NOT NULL) "
                "FROM rps_v2 WHERE version_id = ? GROUP BY trade_date ORDER BY trade_date",
                (VERSION_ID,),
            )
        ]
    finally:
        connection.close()
        b1.close()
    return {
        "version": payload,
        "rows": rows,
        "coverage": coverage_rows,
        "sandbox": str(SANDBOX),
        "sandbox_sha256": sha256_file(SANDBOX),
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="M6.3 RPS v2 重算与核验")
    parser.add_argument("--skip-sandbox", action="store_true", help="复用已存在的 sandbox，仅重跑核验")
    args = parser.parse_args()
    start_db_sha = sha256_file(DB)
    connection = connect()
    try:
        codes, dates, matrix = load_matrix(connection)
        verification = verify_windows(connection, codes, dates, matrix)
        future = check_future_independence(codes, dates, matrix, "2026-09-30")
        legacy_quality = []
        stock_last = dates[-1]
        for day, stock_rows, rps_rows, ret20_rows, ret120_rows in connection.execute(
            """
            SELECT d.trade_date,
                   (SELECT COUNT(*) FROM stock_daily s WHERE s.trade_date = d.trade_date),
                   (SELECT COUNT(*) FROM rps_daily r WHERE r.trade_date = d.trade_date),
                   (SELECT COUNT(*) FROM rps_daily r WHERE r.trade_date = d.trade_date AND r.ret20 IS NOT NULL),
                   (SELECT COUNT(*) FROM rps_daily r WHERE r.trade_date = d.trade_date AND r.ret120 IS NOT NULL)
            FROM (SELECT DISTINCT trade_date FROM rps_daily) d ORDER BY d.trade_date
            """
        ):
            quality = versioning.classify_legacy_day(
                trade_date=date.fromisoformat(day),
                stock_rows=stock_rows,
                rps_rows=rps_rows,
                ret20_rows=ret20_rows,
                ret120_rows=ret120_rows,
                stock_last_date=date.fromisoformat(stock_last),
            )
            if not quality.usable:
                legacy_quality.append(
                    {
                        "trade_date": day,
                        "status": quality.status,
                        "reason": quality.reason,
                        "stock_rows": stock_rows,
                        "rps_rows": rps_rows,
                        "ret20_rows": ret20_rows,
                        "ret120_rows": ret120_rows,
                    }
                )
    finally:
        connection.close()

    sandbox = load_existing_sandbox() if args.skip_sandbox else build_sandbox(connect())
    end_db_sha = sha256_file(DB)

    with (OUT / "rps_window_reconciliation.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(verification[0].keys()))
        writer.writeheader()
        writer.writerows(verification)
    with (OUT / "rps_coverage.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(sandbox["coverage"][0].keys()))
        writer.writeheader()
        writer.writerows(sandbox["coverage"])

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": sandbox["version"],
        "legacy": {
            "rps_daily_status": "LEGACY_UNKNOWN",
            "note": "旧 rps_daily 未写 version_id，本轮不改动；下游必须显式指定 version_id 才能读取",
            "unusable_days": legacy_quality,
        },
        "verification": {
            "sample_dates": list(SAMPLE_DATES),
            "windows": list(versioning.WINDOWS),
            "rows": verification,
            "future_independence": future,
        },
        "sandbox": {
            "path": sandbox["sandbox"],
            "sha256": sandbox["sandbox_sha256"],
            "rows": sandbox["rows"],
            "dates": len(sandbox["coverage"]),
        },
        "db_sha256_before": start_db_sha,
        "db_sha256_after": end_db_sha,
        "db_unchanged": start_db_sha == end_db_sha,
    }
    (OUT / "rps_version_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    worst = min(
        (row["ret_corr"] for row in verification if row.get("ret_corr") is not None),
        default=None,
    )
    print(
        json.dumps(
            {
                "rows": sandbox["rows"],
                "dates": len(sandbox["coverage"]),
                "worst_ret_corr": worst,
                "future": future,
                "db_unchanged": manifest["db_unchanged"],
                "unusable_legacy_days": len(legacy_quality),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
