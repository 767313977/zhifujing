"""M6.2 Batch 5：确定性复现入口（只读）。

对同一数据库 SHA、同一代码、同一参数、同一随机种子，连续运行两次，比较：
1. 冻结变体 A2 的测试集 Bootstrap（500 次、seed 42）与样本指纹；
2. A2 交易级模拟（A_fixed）的权益曲线、逐笔交易与逐日账本指纹；
3. RPS `ret20` 的独立重算（与库内值交叉核验）；
4. breadth 的 SQL 与纯 Python 双实现（单日）。

另做 JSON schema 必填字段校验，并输出实验身份清单。

产物：
- `reports/trend_engine/m62/reproduction_manifest.json`
- `reports/trend_engine/m62/reproduction_diff.csv`

用法：

    python scripts/m62_reproduce.py
"""

from __future__ import annotations

import csv
import hashlib
import json
import platform
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
BACKEND = ROOT / "backend"
for path in (str(BACKEND), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import backtest_trend_engine as bt  # noqa: E402
import m62_market_data_audit as market  # noqa: E402
import run_m61_experiments as exp  # noqa: E402
import run_m61_trade_sim as m61sim  # noqa: E402
from app.models import StockDaily, TradeCalendar  # noqa: E402
from app.services import research_backtest as rb  # noqa: E402
from app.services.trend_engine import trade_sim  # noqa: E402
from sqlalchemy import select  # noqa: E402

OUT = ROOT / "reports/trend_engine/m62"
DB = ROOT / "backend/data/b1_research.db"
CACHE = ROOT / "backend/data/m61_cache/attribution.json"
EXPERIMENT = ROOT / "reports/trend_engine/m61/experiment_results.json"
SEED = 42
ITERATIONS = 500

SCHEMA_RULES = {
    "bootstrap_reproduction.json": {
        "required": ["db", "cache", "m61_frozen_test", "m6_full_sample", "verdict"],
        "nested": {"m61_frozen_test": ["A", "B"]},
    },
    "walk_forward_reproduction.json": {
        "required": ["db", "blocks_requested", "blocks_generated", "models"],
        "nested": {"models": ["A", "B"]},
    },
    "trade_sim_audit.json": {
        "required": ["db", "runs", "reconciliation", "summary"],
        "nested": {"runs": ["A", "B", "A+B"]},
    },
    "market_data_audit.json": {
        "required": ["db", "index", "sector", "rps", "breadth_samples"],
        "nested": {},
    },
    "history_independence.json": {
        "required": ["sources", "code_overlap", "history_experiment", "verdict"],
        "nested": {},
    },
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def canonical_digest(payload) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sample_digest(samples, horizon: int = 20) -> str:
    rows = sorted(
        (str(sample.signal_date), sample.code, round(float(sample.excess), 6))
        for sample in samples
        if sample.horizon == horizon and sample.status == "ok" and sample.excess is not None
    )
    return canonical_digest(rows)


def run_checks(session, matrix, benchmark, records, splits, bars, names, calendar, signals) -> dict:
    run = exp.run(session, matrix, benchmark, records, "A", exp.VARIANTS["A"]["A2"], splits["test"])
    bootstrap = bt._bootstrap(run["samples"], 20, ITERATIONS, SEED)
    bootstrap_alt = bt._bootstrap(run["samples"], 20, ITERATIONS, 7)

    config = m61sim.CONFIGS["A_fixed"]
    sim = trade_sim.simulate(bars_by_code=bars, calendar=calendar, signals=signals, config=config, names=names)
    return {
        "bootstrap_a2_test": bootstrap,
        "bootstrap_seed7": bootstrap_alt,
        "bootstrap_samples_digest": sample_digest(run["samples"]),
        "trade_sim": {
            "metrics": sim.metrics,
            "equity_digest": canonical_digest([[str(day), equity] for day, equity in sim.equity]),
            "ledger_digest": canonical_digest([[str(day), cash, value, equity] for day, cash, value, equity in sim.ledger]),
            "trades_digest": canonical_digest(
                [
                    [trade.code, str(trade.entry_date), str(trade.exit_date), trade.shares, trade.pnl, trade.reason]
                    for trade in sim.trades
                ]
            ),
            "trade_count": len(sim.trades),
            "final_equity": sim.ledger[-1][3] if sim.ledger else None,
        },
    }


def schema_check() -> dict:
    results = {}
    for name, rules in SCHEMA_RULES.items():
        path = OUT / name
        if not path.exists():
            results[name] = {"status": "MISSING"}
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        missing = [key for key in rules["required"] if key not in payload]
        nested_missing = {
            key: [child for child in children if child not in (payload.get(key) or {})]
            for key, children in rules["nested"].items()
        }
        nested_missing = {key: value for key, value in nested_missing.items() if value}
        results[name] = {
            "status": "OK" if not missing and not nested_missing else "INVALID",
            "missing": missing,
            "nested_missing": nested_missing,
        }
    return results


def main() -> int:
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    cache["records_parsed"] = {
        model: [{**item, "day": date.fromisoformat(item["day"])} for item in items]
        for model, items in cache["records"].items()
    }
    records = cache["records_parsed"]
    all_days = sorted({record["day"] for items in records.values() for record in items})
    splits = exp.split_days(all_days)

    session = bt.open_readonly(DB)
    try:
        db_end = session.scalar(select(StockDaily.trade_date).order_by(StockDaily.trade_date.desc()).limit(1))
        calendar = list(
            session.scalars(
                select(TradeCalendar.trade_date).where(TradeCalendar.trade_date <= db_end).order_by(TradeCalendar.trade_date)
            )
        )
        warmup_start = calendar[max(0, calendar.index(all_days[0]) - 120)]
        sim_calendar = [day for day in calendar if all_days[0] <= day <= db_end]
        matrix_start = calendar[max(0, len(calendar) - 260)]
        matrix = rb.load_matrix(session, matrix_start, db_end)
        benchmark = rb.Benchmark(session, matrix, "market")

        signals = m61sim.build_signals(cache, "A", exp.VARIANTS["A"]["A2"], set(splits["test"]))
        codes = sorted({item.code for item in signals})
        bars = m61sim.load_bars_safe(session, codes, warmup_start, db_end)
        names = {
            code: name
            for code, name in session.execute(
                select(StockDaily.code, StockDaily.name).where(
                    StockDaily.trade_date == db_end, StockDaily.code.in_(codes)
                )
            ).all()
        }

        first = run_checks(session, matrix, benchmark, records["A"], splits, bars, names, sim_calendar, signals)
        second = run_checks(session, matrix, benchmark, records["A"], splits, bars, names, sim_calendar, signals)

        connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        try:
            rps_check = market.recompute_ret(connection, "2026-09-30")
            breadth_sql = market.breadth_sql(connection, "2026-09-24")
            breadth_python = market.breadth_python(connection, "2026-09-24")
        finally:
            connection.close()

        diff_rows = [
            {
                "item": "bootstrap_a2_test",
                "run1": canonical_digest(first["bootstrap_a2_test"]),
                "run2": canonical_digest(second["bootstrap_a2_test"]),
                "match": first["bootstrap_a2_test"] == second["bootstrap_a2_test"],
            },
            {
                "item": "bootstrap_samples_digest",
                "run1": first["bootstrap_samples_digest"],
                "run2": second["bootstrap_samples_digest"],
                "match": first["bootstrap_samples_digest"] == second["bootstrap_samples_digest"],
            },
            {
                "item": "trade_sim_equity_digest",
                "run1": first["trade_sim"]["equity_digest"],
                "run2": second["trade_sim"]["equity_digest"],
                "match": first["trade_sim"]["equity_digest"] == second["trade_sim"]["equity_digest"],
            },
            {
                "item": "trade_sim_ledger_digest",
                "run1": first["trade_sim"]["ledger_digest"],
                "run2": second["trade_sim"]["ledger_digest"],
                "match": first["trade_sim"]["ledger_digest"] == second["trade_sim"]["ledger_digest"],
            },
            {
                "item": "trade_sim_trades_digest",
                "run1": first["trade_sim"]["trades_digest"],
                "run2": second["trade_sim"]["trades_digest"],
                "match": first["trade_sim"]["trades_digest"] == second["trade_sim"]["trades_digest"],
            },
            {
                "item": "breadth_sql_vs_python",
                "run1": canonical_digest(breadth_sql),
                "run2": canonical_digest(breadth_python),
                "match": breadth_sql == breadth_python,
            },
            {
                "item": "rps_ret20_recompute",
                "run1": str(rps_check.get("pearson_corr")),
                "run2": str(rps_check.get("median_abs_diff")),
                "match": rps_check.get("status") == "OK" and (rps_check.get("pearson_corr") or 0) > 0.95,
            },
            {
                "item": "bootstrap_seed_sensitivity",
                "run1": canonical_digest(first["bootstrap_a2_test"]),
                "run2": canonical_digest(first["bootstrap_seed7"]),
                "match": first["bootstrap_a2_test"] == first["bootstrap_seed7"],
            },
        ]
        schema = schema_check()
        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "environment": {
                "python": platform.python_version(),
                "sqlite": sqlite3.sqlite_version,
                "platform": platform.platform(),
            },
            "identity": {
                "db": {"path": str(DB), "sha256": sha256_file(DB)},
                "cache": {"path": str(CACHE), "sha256": sha256_file(CACHE)},
                "experiment_results": {"path": str(EXPERIMENT), "sha256": sha256_file(EXPERIMENT)},
                "trade_sim_source": {"path": str(ROOT / "backend/app/services/trend_engine/trade_sim.py"), "sha256": sha256_file(ROOT / "backend/app/services/trend_engine/trade_sim.py")},
                "reproduce_script": {"path": str(Path(__file__)), "sha256": sha256_file(Path(__file__))},
            },
            "config": {
                "variant": "A2",
                "horizon": 20,
                "entry_mode": "next_open",
                "cost_bps": exp.COST_BPS,
                "bootstrap_iterations": ITERATIONS,
                "bootstrap_seed": SEED,
                "trade_rule": "A_fixed",
            },
            "sample": {
                "train": [str(splits["train"][0]), str(splits["train"][-1])],
                "validation": [str(splits["validation"][0]), str(splits["validation"][-1])],
                "test": [str(splits["test"][0]), str(splits["test"][-1])],
                "test_signals": len(signals),
            },
            "results": {"first": first, "second": second},
            "deterministic": all(row["match"] for row in diff_rows if row["item"] != "bootstrap_seed_sensitivity"),
            "seed_sensitivity": {
                "seed42": first["bootstrap_a2_test"],
                "seed7": first["bootstrap_seed7"],
                "stable": first["bootstrap_a2_test"] == first["bootstrap_seed7"],
            },
            "schema": schema,
        }
        (OUT / "reproduction_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )
        with (OUT / "reproduction_diff.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["item", "run1", "run2", "match"])
            writer.writeheader()
            writer.writerows(diff_rows)
        print(json.dumps({"deterministic": manifest["deterministic"], "seed_stable": manifest["seed_sensitivity"]["stable"], "schema": schema}, ensure_ascii=False))
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
