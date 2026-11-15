"""M6.1-C/D 预注册变体实验、Purged Walk-forward 与 Bootstrap（只读）。

使用 `analyze_trend_models_m61.py` 生成的特征缓存（不重新扫描），
在训练/验证/测试三段上评估预注册变体；训练选参、验证确认、冻结后测试一次。

产物：
- `experiment_results.json`
- `05-walk-forward-validation.md`
- `06-bootstrap-analysis.md`

用法：

    python scripts/run_m61_experiments.py --db backend/data/b1_research.db \
        --cache backend/data/m61_cache/attribution.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np
from sqlalchemy import select

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
BACKEND = ROOT / "backend"
for path in (str(BACKEND), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import backtest_trend_engine as bt  # noqa: E402
from app.models import StockDaily, TradeCalendar  # noqa: E402
from app.services import research_backtest as rb  # noqa: E402

HORIZONS = (5, 10, 20, 40, 60)
COST_BPS = 15.0
TOP = 20

#: 预注册变体：权重 + 过滤条件。测试集在冻结前不得参与选择。
VARIANTS = {
    "A": {
        "A0": {"weights": (40, 30, 30), "rps": 3.0, "filters": {}},
        "A1": {
            "weights": (50, 30, 20),
            "rps": 0.0,
            "filters": {
                "distance_ma20": ("<=", 10.0),
                "ret20": ("<=", 30.0),
                "amount_ratio20": ("<=", 5.0),
                "trend_days": ("range", 6, 60),
            },
        },
        "A2": {
            "weights": (50, 30, 20),
            "rps": 0.0,
            "filters": {
                "distance_ma20": ("<=", 10.0),
                "ret20": ("<=", 30.0),
                "amount_ratio20": ("<=", 5.0),
                "trend_days": ("range", 6, 60),
                "_regime": ("in", ("BULL", "SIDEWAYS")),
            },
        },
        "A3": {
            "weights": (50, 30, 20),
            "rps": 0.0,
            "filters": {
                "distance_ma20": ("<=", 10.0),
                "ret20": ("<=", 30.0),
                "amount_ratio20": ("<=", 5.0),
                "trend_days": ("range", 6, 60),
                "convergence": ("<=", 6.0),
                "platform_width": ("<=", 12.0),
                "close_position": (">=", 0.7),
            },
        },
    },
    "B": {
        "B0": {"weights": (40, 30, 30), "rps": 3.0, "filters": {}},
        "B1": {
            "weights": (50, 30, 20),
            "rps": 0.0,
            "filters": {
                "trend_days": ("<=", 60),
                "distance_ma20": ("<=", 8.0),
                "pullback_atr": (">=", 1.2),
            },
        },
        "B2": {
            "weights": (50, 30, 20),
            "rps": 0.0,
            "filters": {
                "trend_days": ("<=", 60),
                "distance_ma20": ("<=", 8.0),
                "pullback_atr": (">=", 1.2),
                "pullback_shrink": ("<=", 0.8),
            },
        },
        "B3": {
            "weights": (50, 30, 20),
            "rps": 0.0,
            "filters": {
                "trend_days": ("<=", 60),
                "distance_ma20": ("<=", 8.0),
                "pullback_atr": (">=", 1.2),
                "_regime": ("!=", "SIDEWAYS"),
            },
        },
    },
}


def split_days(days: list[date]) -> dict[str, list[date]]:
    size = len(days)
    a = int(size * 0.6)
    b = int(size * 0.8)
    return {"train": days[:a], "validation": days[a:b], "test": days[b:]}


def passes(record: dict, filters: dict) -> bool:
    features = record.get("features") or {}
    for key, rule in filters.items():
        if key == "_regime":
            value = record.get("regime") or "UNKNOWN"
        else:
            value = features.get(key)
        if value is None:
            return False
        op = rule[0]
        if op == "<=" and not float(value) <= float(rule[1]):
            return False
        if op == ">=" and not float(value) >= float(rule[1]):
            return False
        if op == "range" and not (float(rule[1]) <= float(value) <= float(rule[2])):
            return False
        if op == "in" and value not in rule[1]:
            return False
        if op == "!=" and value == rule[1]:
            return False
    return True


def score(record: dict, model: str, spec: dict) -> float:
    parts = record["parts"]
    keys = ("structure", "breakout", "volume") if model == "A" else ("structure", "pullback", "volume")
    w1, w2, w3 = spec["weights"]
    weights = zip(keys, (w1, w2, w3))
    total = 0.0
    used = 0
    for key, weight in weights:
        used += weight
        value = parts.get(key)
        if value is None:
            continue
        total += max(0.0, min(1.0, float(value))) * weight
    if used <= 0:
        return 0.0
    total = total / used * (100 - spec["rps"])
    if spec["rps"] > 0 and parts.get("rps") is not None:
        total += float(parts["rps"]) * spec["rps"]
    return round(total, 1)


def signals_for(records, model, spec, days: set[date]) -> list:
    grouped: dict[date, dict[str, float]] = defaultdict(dict)
    for record in records:
        if record["day"] not in days or not passes(record, spec["filters"]):
            continue
        value = score(record, model, spec)
        code = record["code"]
        if code not in grouped[record["day"]] or value > grouped[record["day"]][code]:
            grouped[record["day"]][code] = value
    signals = []
    for day in sorted(grouped):
        picked = sorted(grouped[day].items(), key=lambda item: (-item[1], item[0]))[:TOP]
        for code, value in picked:
            signals.append(rb.Signal(code=code, signal_date=day, meta={"score": value}))
    return signals


def run(session, matrix, benchmark, records, model, spec, days) -> dict:
    signals = signals_for(records, model, spec, set(days))
    result = rb.run_signals(
        session,
        signals,
        strategy=f"{model}-variant",
        params={"top": TOP, "weights": spec["weights"], "rps": spec["rps"], "filters": spec["filters"]},
        horizons=HORIZONS,
        entry_mode="next_open",
        cost_bps=COST_BPS,
        matrix=matrix,
        benchmark=benchmark,
    )
    out = {"signals": len(signals), "by_horizon": {}}
    for horizon in HORIZONS:
        rows = [
            sample
            for sample in result.samples
            if sample.horizon == horizon and sample.status == "ok" and sample.gross is not None
        ]
        if not rows:
            out["by_horizon"][horizon] = {"samples": 0}
            continue
        gross = [row.gross for row in rows]
        excess = [row.excess for row in rows if row.excess is not None]
        out["by_horizon"][horizon] = {
            "samples": len(rows),
            "mean": round(float(np.mean(gross)), 2),
            "median": round(float(np.median(gross)), 2),
            "win_pct": round(float(np.mean([1 if value > 0 else 0 for value in gross]) * 100), 1),
            "excess_mean": round(float(np.mean(excess)), 2) if excess else None,
            "excess_t_p": _t_pvalue(excess),
            "profit_factor": bt._profit_factor(gross),
            "max_drawdown_points": bt._max_drawdown(gross),
            "tail_top5_share": bt._tail(rows).get("top5_share"),
            "mean_excluding_top5pct": bt._tail(rows).get("mean_excluding_top5pct"),
        }
    out["samples"] = result.samples
    return out


def _t_pvalue(values: list[float]) -> float | None:
    if len(values) < 10:
        return None
    arr = np.asarray(values, dtype=float)
    std = float(arr.std(ddof=1))
    if std <= 0:
        return None
    t = float(arr.mean() / (std / math.sqrt(len(arr))))
    return round(math.erfc(abs(t) / math.sqrt(2)), 6)


def purged_blocks(days: list[date], blocks: int = 4, embargo: int = 20) -> list[dict]:
    size = len(days)
    edges = [int(size * index / blocks) for index in range(blocks + 1)]
    out = []
    for index in range(1, blocks):
        start, end = edges[index], edges[index + 1]
        embargo_start = max(0, start - embargo)
        out.append(
            {
                "test_start": str(days[start]),
                "test_end": str(days[end - 1]),
                "embargo_start": str(days[embargo_start]),
                "test_days": [day for day in days[start:end]],
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="M6.1 预注册变体实验（只读）")
    parser.add_argument("--db", default="backend/data/b1_research.db")
    parser.add_argument("--cache", default="backend/data/m61_cache/attribution.json")
    parser.add_argument("--report-dir", default="reports/trend_engine/m61")
    parser.add_argument("--bootstrap", type=int, default=500)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    payload = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    records = {
        model: [
            {**item, "day": date.fromisoformat(item["day"])}
            for item in items
        ]
        for model, items in payload["records"].items()
    }
    all_days = sorted({record["day"] for items in records.values() for record in items})
    splits = split_days(all_days)

    session = bt.open_readonly(db_path)
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

    report = {
        "db": str(db_path),
        "cache": str(Path(args.cache).resolve()),
        "scan_start": str(all_days[0]),
        "scan_end": str(all_days[-1]),
        "splits": {name: [str(days[0]), str(days[-1])] for name, days in splits.items() if days},
        "models": {},
        "multiple_testing": {},
    }
    variants_tested = 0

    for model in ("A", "B"):
        items = records.get(model) or []
        if not items:
            continue
        model_node = {"variants": {}}
        for name, spec in VARIANTS[model].items():
            variants_tested += 1
            node = {}
            for split_name, days in splits.items():
                run_result = run(session, matrix, benchmark, items, model, spec, days)
                node[split_name] = {
                    "signals": run_result["signals"],
                    "20": run_result["by_horizon"].get(20),
                    "60": run_result["by_horizon"].get(60),
                }
            model_node["variants"][name] = node

        # 选择：训练集 20 日超额最高（样本 ≥200），再看验证
        eligible = [
            (name, node["train"]["20"])
            for name, node in model_node["variants"].items()
            if node["train"]["20"].get("samples", 0) >= 200
            and node["train"]["20"].get("excess_mean") is not None
        ]
        if not eligible:
            model_node["selection"] = {"status": "UNKNOWN", "reason": "训练集没有足够样本"}
            report["models"][model] = model_node
            continue
        best_name, best_stats = max(eligible, key=lambda item: item[1]["excess_mean"])
        best = model_node["variants"][best_name]
        validation = best["validation"]["20"]
        frozen = {
            "variant": best_name,
            "train_excess": best_stats["excess_mean"],
            "validation_excess": validation.get("excess_mean"),
            "validation_samples": validation.get("samples"),
            "passed_validation": (validation.get("excess_mean") or -1) > 0,
        }
        model_node["selection"] = frozen

        # 冻结后测试集（只评一次）
        test_run = run(session, matrix, benchmark, items, model, VARIANTS[model][best_name], splits["test"])
        test_samples = test_run["samples"]
        bootstrap = bt._bootstrap(test_samples, 20, args.bootstrap, args.seed)
        test_20 = test_run["by_horizon"].get(20)
        model_node["frozen_test"] = {
            "20": test_20,
            "bootstrap_20": bootstrap,
            "passed": bool(
                frozen["passed_validation"]
                and (test_20 or {}).get("samples", 0) >= 200
                and (test_20 or {}).get("excess_mean", 0) > 0
                and (bootstrap or {}).get("ci_low", -1) > 0
            ),
        }

        # Purged Walk-forward：4 块，测试块前 20 个交易日为 embargo（不参与评估）
        blocks = purged_blocks(all_days, blocks=4, embargo=20)
        wf = []
        for block in blocks:
            block_run = run(
                session,
                matrix,
                benchmark,
                items,
                model,
                VARIANTS[model][best_name],
                block["test_days"],
            )
            wf.append(
                {
                    **{key: block[key] for key in ("test_start", "test_end", "embargo_start")},
                    "20": block_run["by_horizon"].get(20),
                    "60": block_run["by_horizon"].get(60),
                }
            )
        model_node["purged_walk_forward"] = wf
        report["models"][model] = model_node

    report["multiple_testing"] = {
        "variants_tested": variants_tested,
        "bonferroni_alpha": round(0.05 / max(variants_tested, 1), 5),
    }

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "experiment_results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    write_walk_forward(report_dir / "05-walk-forward-validation.md", report)
    write_bootstrap(report_dir / "06-bootstrap-analysis.md", report)
    print(f"实验完成：{report_dir}")
    return 0


def write_walk_forward(path: Path, report: dict) -> None:
    lines = ["# M6.1-D Purged Walk-forward 验证\n"]
    lines.append(f"- 数据库：`{report['db']}`")
    lines.append(f"- 区间：{report['scan_start']} ~ {report['scan_end']}")
    lines.append(f"- 分段：{report['splits']}")
    lines.append("- 规则：训练 60% 选变体 → 验证 20% 确认 → 冻结 → 测试 20% 只评一次；")
    lines.append("  Purged WF 用 4 个连续测试块，每块前 20 个交易日为 embargo（不参与评估）\n")

    for model, node in report["models"].items():
        lines.append(f"## Model {model}\n")
        lines.append("### 预注册变体（20 日超额）\n")
        lines.append("| 变体 | 训练样本 | 训练超额 | 验证样本 | 验证超额 | 测试样本 | 测试超额 |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
        for name, variant in node["variants"].items():
            lines.append(
                f"| {name} | {variant['train']['20'].get('samples')} | {variant['train']['20'].get('excess_mean')} | "
                f"{variant['validation']['20'].get('samples')} | {variant['validation']['20'].get('excess_mean')} | "
                f"{variant['test']['20'].get('samples')} | {variant['test']['20'].get('excess_mean')} |"
            )
        lines.append("")
        selection = node.get("selection", {})
        lines.append(
            f"- 选择：{selection.get('variant')}｜训练 {selection.get('train_excess')}｜"
            f"验证 {selection.get('validation_excess')}｜通过验证：{selection.get('passed_validation')}"
        )
        frozen = node.get("frozen_test") or {}
        lines.append(
            f"- 冻结后测试：样本 {frozen.get('20', {}).get('samples')}，"
            f"超额 {frozen.get('20', {}).get('excess_mean')}，"
            f"bootstrap CI {frozen.get('bootstrap_20')}，通过：{frozen.get('passed')}"
        )
        lines.append("")
        lines.append("### Purged Walk-forward 块\n")
        lines.append("| 测试块 | embargo 起点 | 样本 | 20 日超额 | 60 日超额 |")
        lines.append("| --- | --- | ---: | ---: | ---: |")
        for block in node.get("purged_walk_forward", []):
            lines.append(
                f"| {block['test_start']} ~ {block['test_end']} | {block['embargo_start']} | "
                f"{block['20'].get('samples')} | {block['20'].get('excess_mean')} | {block['60'].get('excess_mean')} |"
            )
        lines.append("")

    lines.append("## 多重检验\n")
    lines.append(f"- 变体数：{report['multiple_testing']['variants_tested']}")
    lines.append(f"- Bonferroni α：{report['multiple_testing']['bonferroni_alpha']}")
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_bootstrap(path: Path, report: dict) -> None:
    lines = ["# M6.1-D 日期聚类 Bootstrap\n"]
    lines.append("- 以交易日为重采样单位（同日多只票视为一个簇），避免把同日样本当独立观测。")
    lines.append("- 指标：20 日超额均值的 95% 区间；区间下界 >0 才满足准入。\n")
    lines.append("| 模型 | 冻结变体 | 有效交易日 | 均值 | CI 下界 | CI 上界 | 结论 |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | --- |")
    for model, node in report["models"].items():
        frozen = node.get("frozen_test") or {}
        boot = frozen.get("bootstrap_20") or {}
        verdict = "通过" if (boot.get("ci_low") or -1) > 0 else "未通过"
        lines.append(
            f"| {model} | {(node.get('selection') or {}).get('variant')} | {boot.get('days')} | "
            f"{boot.get('mean')} | {boot.get('ci_low')} | {boot.get('ci_high')} | {verdict} |"
        )
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
