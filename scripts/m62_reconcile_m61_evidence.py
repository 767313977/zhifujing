"""M6.2 Batch 1：M6.1 统计证据追溯与独立复算（只读）。

核对三处冲突：
1. 总验收报告的 A/B Bootstrap CI 与 `06-bootstrap-analysis.md` 不一致；
2. Purged Walk-forward 文字称 4 块、表格只有 3 块；
3. A2 验证未通过却仍展示测试集结果。

做法：
- 用 M6.1 的特征缓存重放冻结变体 A2/B2 的测试集，重算 `bt._bootstrap`（500 次、seed 42），
  与 `experiment_results.json` 逐字段比对；
- 重算 `purged_blocks` 与每个测试块的评估，记录真实区块数、样本簇、标签越界；
- 对 M6 全样本 CI 只做字段级追溯（M6 未落盘逐样本记录，无法离线重算）。

产物：
- `bootstrap_reproduction.json`
- `walk_forward_reproduction.json`
- `evidence_traceability.csv`

用法：

    python scripts/m62_reconcile_m61_evidence.py \
        --db backend/data/b1_research.db \
        --cache backend/data/m61_cache/attribution.json \
        --out-dir reports/trend_engine/m62
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import select

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
BACKEND = ROOT / "backend"
for path in (str(BACKEND), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import backtest_trend_engine as bt  # noqa: E402
import run_m61_experiments as exp  # noqa: E402
from app.models import StockDaily, TradeCalendar  # noqa: E402
from app.services import research_backtest as rb  # noqa: E402

HORIZON = 20
BOOTSTRAP_ITERATIONS = 500
BOOTSTRAP_SEED = 42


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def git_sha(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "UNKNOWN"


def round_or_none(value, digits: int = 2):
    return None if value is None else round(float(value), digits)


def describe_days(samples) -> dict:
    """把 20 日样本按 signal_date 聚类，统计簇大小与重复情况。"""
    rows = [s for s in samples if s.horizon == HORIZON and s.status == "ok" and s.excess is not None]
    counts = Counter(s.signal_date for s in rows)
    pairs = Counter((s.signal_date, s.code) for s in rows)
    return {
        "samples": len(rows),
        "distinct_days": len(counts),
        "dates": [str(day) for day in sorted(counts)],
        "per_day_counts": {str(day): counts[day] for day in sorted(counts)},
        "max_per_day": max(counts.values()) if counts else 0,
        "duplicate_signal_pairs": sum(1 for value in pairs.values() if value > 1),
    }


def label_overflow(samples, test_end: date, horizon: int) -> int:
    rows = [
        s
        for s in samples
        if s.horizon == horizon and s.status == "ok" and s.exit_date is not None and s.exit_date > test_end
    ]
    return len(rows)


def reproduce_frozen_test(session, matrix, benchmark, records, model: str, variant: str, days) -> dict:
    run_result = exp.run(session, matrix, benchmark, records, model, exp.VARIANTS[model][variant], days)
    samples = run_result["samples"]
    bootstrap = bt._bootstrap(samples, HORIZON, BOOTSTRAP_ITERATIONS, BOOTSTRAP_SEED)
    stats = describe_days(samples)
    stats["horizon20"] = run_result["by_horizon"].get(HORIZON)
    return {"bootstrap": bootstrap, "stats": stats}


def compare(expected: dict, reproduced: dict) -> dict:
    keys = ("days", "mean", "ci_low", "ci_high")
    diffs = {
        key: {"expected": expected.get(key), "reproduced": reproduced.get(key)}
        for key in keys
        if expected.get(key) != reproduced.get(key)
    }
    return {"match": not diffs, "differences": diffs}


def reproduce_walk_forward(session, matrix, benchmark, records, model: str, variant: str, blocks) -> list[dict]:
    out = []
    for block in blocks:
        days = block["test_days"]
        run_result = exp.run(session, matrix, benchmark, records, model, exp.VARIANTS[model][variant], days)
        samples = run_result["samples"]
        stats = describe_days(samples)
        out.append(
            {
                "test_start": block["test_start"],
                "test_end": block["test_end"],
                "embargo_start": block["embargo_start"],
                "test_days": len(days),
                "horizon20": run_result["by_horizon"].get(HORIZON),
                "horizon60": run_result["by_horizon"].get(60),
                "distinct_days_20": stats["distinct_days"],
                "duplicate_signal_pairs": stats["duplicate_signal_pairs"],
                "samples_beyond_test_end_20": label_overflow(samples, date.fromisoformat(block["test_end"]), HORIZON),
                "samples_beyond_test_end_60": label_overflow(samples, date.fromisoformat(block["test_end"]), 60),
            }
        )
    return out


def csv_rows(payload: dict, git_head: str, db_sha: str, m61_sha: str) -> list[dict]:
    rows: list[dict] = []
    for model, node in payload["m61_frozen_test"].items():
        rows.append(
            {
                "metric": f"{model} 冻结测试 20 日超额 Bootstrap CI",
                "report": "reports/trend_engine/m61/06-bootstrap-analysis.md",
                "report_value": f"[{node['expected']['ci_low']}, {node['expected']['ci_high']}]",
                "json_path": f"experiment_results.json#/models/{model}/frozen_test/bootstrap_20",
                "json_value": json.dumps(node["expected"], ensure_ascii=False),
                "script": "scripts/run_m61_experiments.py",
                "function": "main → backtest_trend_engine._bootstrap",
                "data_slice": f"test {node['slice']['start']}~{node['slice']['end']}；{node['stats']['distinct_days']} 个信号日",
                "db": payload["db"]["path"],
                "db_sha256": db_sha,
                "git_sha": m61_sha,
                "reproduced": json.dumps(node["reproduced"], ensure_ascii=False),
                "status": "REPRODUCED_MATCH" if node["comparison"]["match"] else "REPRODUCED_MISMATCH",
                "git_head": git_head,
            }
        )
    for model, node in payload["m6_full_sample"].items():
        rows.append(
            {
                "metric": f"{model} M6 全样本 20 日超额 Bootstrap CI",
                "report": "reports/trend_engine/m61/09-m61-final-acceptance.md#4",
                "report_value": node["report_value"],
                "json_path": node["json_path"],
                "json_value": json.dumps(node["json_value"], ensure_ascii=False),
                "script": "scripts/backtest_trend_engine.py",
                "function": "summarize → _bootstrap",
                "data_slice": node["data_slice"],
                "db": payload["db"]["path"],
                "db_sha256": db_sha,
                "git_sha": "dbf7dd2 (M6)",
                "reproduced": "N/A",
                "status": node["status"],
                "git_head": git_head,
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="M6.2 M6.1 证据追溯（只读）")
    parser.add_argument("--db", default="backend/data/b1_research.db")
    parser.add_argument("--cache", default="backend/data/m61_cache/attribution.json")
    parser.add_argument("--m61", default="reports/trend_engine/m61/experiment_results.json")
    parser.add_argument("--m6", default="reports/trend_engine/m6/m6_summary.json")
    parser.add_argument("--out-dir", default="reports/trend_engine/m62")
    args = parser.parse_args()

    db_path = (ROOT / args.db).resolve()
    cache_path = (ROOT / args.cache).resolve()
    m61_path = (ROOT / args.m61).resolve()
    m6_path = (ROOT / args.m6).resolve()
    out_dir = (ROOT / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    m61 = json.loads(m61_path.read_text(encoding="utf-8"))
    m6 = json.loads(m6_path.read_text(encoding="utf-8"))
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    records = {
        model: [{**item, "day": date.fromisoformat(item["day"])} for item in items]
        for model, items in payload["records"].items()
    }
    all_days = sorted({record["day"] for items in records.values() for record in items})
    splits = exp.split_days(all_days)

    db_sha = sha256_file(db_path)
    git_head = git_sha("rev-parse", "HEAD")
    m61_sha = git_sha("rev-parse", "HEAD:scripts/run_m61_experiments.py")

    session = bt.open_readonly(db_path)
    try:
        db_end = session.scalar(select(StockDaily.trade_date).order_by(StockDaily.trade_date.desc()).limit(1))
        calendar = list(
            session.scalars(select(TradeCalendar.trade_date).where(TradeCalendar.trade_date <= db_end))
            if bt.has_table(session, "trade_calendar")
            else []
        )
        if not calendar:
            calendar = sorted(set(session.scalars(select(StockDaily.trade_date))))
        matrix_start = calendar[max(0, len(calendar) - 260)]
        matrix = rb.load_matrix(session, matrix_start, db_end)
        benchmark = rb.Benchmark(session, matrix, "market")

        bootstrap_payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "db": {"path": str(db_path), "sha256": db_sha},
            "cache": {"path": str(cache_path), "sha256": sha256_file(cache_path)},
            "m61_experiment_results": {"path": str(m61_path), "sha256": sha256_file(m61_path)},
            "m6_summary": {"path": str(m6_path), "sha256": sha256_file(m6_path)},
            "git": {"head": git_head, "m61_experiments_blob": m61_sha},
            "statistical_contract": {
                "resampling_unit": "signal_date（同日多只股票为一个簇）",
                "ci_method": "percentile：排序后取 2.5% / 97.5% 分位",
                "iterations": BOOTSTRAP_ITERATIONS,
                "seed": BOOTSTRAP_SEED,
                "horizon_days": HORIZON,
                "rounding": 3,
                "entry_mode": "next_open",
                "cost_bps": exp.COST_BPS,
            },
            "m61_frozen_test": {},
            "m6_full_sample": {},
            "verdict": {},
        }
        wf_payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "db": {"path": str(db_path), "sha256": db_sha},
            "git": {"head": git_head, "m61_experiments_blob": m61_sha},
            "blocks_requested": 4,
            "blocks_generated": 0,
            "embargo_days": 20,
            "note": "purged_blocks(days, blocks=4) 只返回 index 1..3；首块被当作初始训练段，无独立训练，embargo 仅记录不作训练剔除。",
            "models": {},
        }

        for model in ("A", "B"):
            selection = m61["models"][model]["selection"]
            variant = selection["variant"]
            expected_boot = m61["models"][model]["frozen_test"]["bootstrap_20"]
            reproduced = reproduce_frozen_test(session, matrix, benchmark, records[model], model, variant, splits["test"])
            bootstrap_payload["m61_frozen_test"][model] = {
                "variant": variant,
                "slice": {"start": str(splits["test"][0]), "end": str(splits["test"][-1]), "days": len(splits["test"])},
                "expected": expected_boot,
                "reproduced": reproduced["bootstrap"],
                "comparison": compare(expected_boot, reproduced["bootstrap"]),
                "stats": {key: reproduced["stats"][key] for key in ("samples", "distinct_days", "dates", "per_day_counts", "max_per_day", "duplicate_signal_pairs")},
                "horizon20": reproduced["stats"]["horizon20"],
                "frozen_test_passed_flag": m61["models"][model]["frozen_test"]["passed"],
            }

            blocks = exp.purged_blocks(all_days, blocks=4, embargo=20)
            wf_payload["blocks_generated"] = len(blocks)
            stored = m61["models"][model]["purged_walk_forward"]
            recomputed = reproduce_walk_forward(session, matrix, benchmark, records[model], model, variant, blocks)
            comparisons = []
            for index, item in enumerate(recomputed):
                base = stored[index] if index < len(stored) else {}
                comparisons.append(
                    {
                        "block": index + 1,
                        "expected": {
                            "samples": (base.get("20") or {}).get("samples"),
                            "excess_mean_20": (base.get("20") or {}).get("excess_mean"),
                            "excess_mean_60": (base.get("60") or {}).get("excess_mean"),
                            "test_start": base.get("test_start"),
                            "test_end": base.get("test_end"),
                        },
                        "reproduced": {
                            "samples": (item["horizon20"] or {}).get("samples"),
                            "excess_mean_20": (item["horizon20"] or {}).get("excess_mean"),
                            "excess_mean_60": (item["horizon60"] or {}).get("excess_mean"),
                            "test_start": item["test_start"],
                            "test_end": item["test_end"],
                        },
                        "match": (base.get("20") or {}).get("samples") == (item["horizon20"] or {}).get("samples")
                        and (base.get("20") or {}).get("excess_mean") == (item["horizon20"] or {}).get("excess_mean")
                        and (base.get("60") or {}).get("excess_mean") == (item["horizon60"] or {}).get("excess_mean"),
                        "detail": item,
                    }
                )
            wf_payload["models"][model] = {"variant": variant, "blocks": comparisons}

        for model in ("A", "B"):
            node = m6["models"][model]["full"]["bootstrap"][str(HORIZON)]
            bootstrap_payload["m6_full_sample"][model] = {
                "report_value": "[-1.53, 0.30]" if model == "A" else "[-1.61, 0.14]",
                "json_path": f"m6_summary.json#/models/{model}/full/bootstrap/20",
                "json_value": node,
                "source_table": "reports/trend_engine/stages/06-m6.md#2.4",
                "data_slice": f"M6 全样本 {m6['scan_start']}~{m6['scan_end']}（{node['days']} 个有效信号日，1000 次迭代）",
                "status": "TRACED_NOT_REEXECUTED",
                "reason": "M6 未落盘逐样本记录，无法离线重算；数值与 JSON 在三位小数舍入后一致，因此不是计算错误，而是被误标为 M6.1 结果。",
            }

        bootstrap_payload["verdict"] = {
            "m61_bootstrap_reproduced": all(
                node["comparison"]["match"] for node in bootstrap_payload["m61_frozen_test"].values()
            ),
            "m61_bootstrap_source": "M6.1 冻结测试集（A2/B2），非 M6 全样本",
            "acceptance_report_values_source": "M6 全样本 /models/*/full/bootstrap/20，被 09 号验收报告误当作 M6.1 bootstrap 引用",
            "a_test_days": bootstrap_payload["m61_frozen_test"]["A"]["stats"]["distinct_days"],
            "b_test_days": bootstrap_payload["m61_frozen_test"]["B"]["stats"]["distinct_days"],
            "wf_blocks_actual": wf_payload["blocks_generated"],
        }

        (out_dir / "bootstrap_reproduction.json").write_text(
            json.dumps(bootstrap_payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )
        (out_dir / "walk_forward_reproduction.json").write_text(
            json.dumps(wf_payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )

        rows = csv_rows(bootstrap_payload, git_head, db_sha, m61_sha)
        for model, node in wf_payload["models"].items():
            matched = sum(1 for item in node["blocks"] if item["match"])
            rows.append(
                {
                    "metric": f"{model} Purged WF 测试块数量",
                    "report": "reports/trend_engine/m61/05-walk-forward-validation.md",
                    "report_value": "文字称 4 块，表格 3 块",
                    "json_path": f"experiment_results.json#/models/{model}/purged_walk_forward",
                    "json_value": f"{len(node['blocks'])} 块",
                    "script": "scripts/run_m61_experiments.py",
                    "function": "purged_blocks(days, blocks=4, embargo=20)",
                    "data_slice": f"{len(all_days)} 个缓存交易日",
                    "db": str(db_path),
                    "db_sha256": db_sha,
                    "git_sha": m61_sha,
                    "reproduced": f"{matched}/{len(node['blocks'])} 块指标一致",
                    "status": "REPRODUCED_MATCH" if matched == len(node["blocks"]) else "REPRODUCED_MISMATCH",
                    "git_head": git_head,
                }
            )
        with (out_dir / "evidence_traceability.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

        print(
            json.dumps(
                {
                    "m61_bootstrap_reproduced": bootstrap_payload["verdict"]["m61_bootstrap_reproduced"],
                    "wf_blocks": wf_payload["blocks_generated"],
                    "wf_match": {
                        model: [item["match"] for item in node["blocks"]]
                        for model, node in wf_payload["models"].items()
                    },
                },
                ensure_ascii=False,
            )
        )
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
