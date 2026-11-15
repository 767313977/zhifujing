"""M6.1-B Model A/B 失效归因（只读）。

流程：

1. 扫描 240 个交易日的**全部合格信号**，保存特征（价格/均线/RPS/量价/板块/环境/行业/市值）；
2. 用统一内核做 T+1 开盘成交模拟（5/10/20/40/60 日）；
3. 输出收益分布、MFE/MAE、尾部贡献、环境/行业/市值/趋势阶段分层、
   因子分组 IC 与十分位收益、单因子选股、去一因子（drop-one）消融、最差 vs 最好样本特征差异；
4. 给出「为什么能抓到大牛股但没有整体超额」的证据。

所有数据库访问 `mode=ro`；特征缓存写在 `backend/data/m61_cache/`（gitignore 内，不提交）。

用法：

    python scripts/analyze_trend_models_m61.py --db backend/data/b1_research.db --days 240
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
from app.models import StockBasic, StockDaily, TradeCalendar  # noqa: E402
from app.services import research_backtest as rb  # noqa: E402
from app.services.trend_engine import config as te_config  # noqa: E402

HORIZONS = (5, 10, 20, 40, 60)


# ---------------------------------------------------------------- 数据与缓存


def load_or_scan(session, db_path: Path, days: int, cache_path: Path, reuse: bool) -> dict:
    if reuse and cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    db_end = session.scalar(select(StockDaily.trade_date).order_by(StockDaily.trade_date.desc()).limit(1))
    calendar = (
        list(
            session.scalars(
                select(TradeCalendar.trade_date)
                .where(TradeCalendar.trade_date <= db_end)
                .order_by(TradeCalendar.trade_date)
            )
        )
        if bt.has_table(session, "trade_calendar")
        else sorted(set(session.scalars(select(StockDaily.trade_date))))
    )
    scan_days = calendar[-days:]
    matrix_start = calendar[max(0, calendar.index(scan_days[0]) - 140)]
    matrix = rb.load_matrix(session, matrix_start, db_end)
    rps_matrix = bt.load_rps_matrix(session, matrix.calendar, matrix.codes)
    series = bt.build_series(matrix, 60)
    sector_map = bt.build_sector_map(session, scan_days, "kph_selected")
    records = bt.scan(
        session,
        matrix,
        rps_matrix,
        series,
        set(scan_days),
        sector_map=sector_map,
        collect_features=True,
    )

    # 行业与市值（当前快照，报告中标注非 PIT）
    basics = (
        {
            row.code: (row.industry, row.total_mv, row.asof)
            for row in session.scalars(select(StockBasic)).all()
        }
        if bt.has_table(session, "stock_basic")
        else {}
    )
    regimes = bt.build_regime_map(session, matrix.calendar, "000001.SH")
    for model in records:
        for record in records[model]:
            industry, total_mv, _asof = basics.get(record["code"], (None, None, None))
            record["industry"] = industry
            record["total_mv"] = total_mv
            record["regime"] = regimes.get(record["day"], "UNKNOWN")

    payload = {
        "db": str(db_path),
        "scan_start": str(scan_days[0]),
        "scan_end": str(scan_days[-1]),
        "days": len(scan_days),
        "records": {
            model: [
                {**record, "day": str(record["day"])}
                for record in items
            ]
            for model, items in records.items()
        },
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    return payload


def restore_records(payload: dict) -> dict[str, list[dict]]:
    return {
        model: [{**record, "day": date.fromisoformat(record["day"])} for record in items]
        for model, items in payload["records"].items()
    }


# ---------------------------------------------------------------- 成交与样本


def run_all(session, matrix, benchmark, records, model, config, top=50) -> tuple[dict, list]:
    signals = bt.build_signals(
        records,
        configs={"A": config, "B": config},
        rps_points=config.rps_points,
        sector_points=0.0,
        top=top,
        min_score=0.0,
    )
    result = rb.run_signals(
        session,
        signals,
        strategy=f"attribution-{model}",
        params={"top": top, "model": model},
        horizons=HORIZONS,
        entry_mode="next_open",
        cost_bps=15.0,
        matrix=matrix,
        benchmark=benchmark,
    )
    return result, signals


def execution(samples, horizon):
    return [
        sample
        for sample in samples
        if sample.horizon == horizon and sample.status == "ok" and sample.gross is not None
    ]


def _skew(values: list[float]) -> float | None:
    if len(values) < 3:
        return None
    arr = np.asarray(values, dtype=float)
    std = float(arr.std(ddof=1))
    if std <= 0:
        return None
    return round(float(((arr - arr.mean()) ** 3).mean() / std**3), 2)


def _t_pvalue(values: list[float]) -> float | None:
    if len(values) < 10:
        return None
    arr = np.asarray(values, dtype=float)
    std = float(arr.std(ddof=1))
    if std <= 0:
        return None
    t = float(arr.mean() / (std / math.sqrt(len(arr))))
    p = math.erfc(abs(t) / math.sqrt(2))
    return round(p, 6)


def stats(samples, horizon: int) -> dict:
    rows = execution(samples, horizon)
    if not rows:
        return {"samples": 0}
    gross = [row.gross for row in rows]
    excess = [row.excess for row in rows if row.excess is not None]
    mfe = [row.mfe for row in rows if row.mfe is not None]
    mae = [row.mae for row in rows if row.mae is not None]
    tail = bt._tail(rows)
    return {
        "samples": len(rows),
        "mean": round(float(np.mean(gross)), 2),
        "median": round(float(np.median(gross)), 2),
        "win_pct": round(float(np.mean([1 if value > 0 else 0 for value in gross]) * 100), 1),
        "excess_mean": round(float(np.mean(excess)), 2) if excess else None,
        "excess_median": round(float(np.median(excess)), 2) if excess else None,
        "excess_t_p": _t_pvalue(excess),
        "profit_factor": bt._profit_factor(gross),
        "max_drawdown_points": bt._max_drawdown(gross),
        "max_consecutive_losses": bt._max_consecutive_losses(gross),
        "mfe_mean": round(float(np.mean(mfe)), 2) if mfe else None,
        "mae_mean": round(float(np.mean(mae)), 2) if mae else None,
        "mfe_mae_ratio": round(float(np.mean(mfe)) / abs(float(np.mean(mae))), 2)
        if mfe and mae and float(np.mean(mae)) != 0
        else None,
        "skew": _skew(gross),
        "p10": round(float(np.percentile(gross, 10)), 2),
        "p90": round(float(np.percentile(gross, 90)), 2),
        "tail_top5_share": tail.get("top5_share"),
        "mean_excluding_top5pct": tail.get("mean_excluding_top5pct"),
        "top1_contribution": _contribution(gross, 0.01),
        "top5_contribution": _contribution(gross, 0.05),
        "top10_contribution": _contribution(gross, 0.10),
    }


def _contribution(values: list[float], ratio: float) -> float | None:
    ordered = sorted(values, reverse=True)
    count = max(1, int(len(ordered) * ratio))
    total = sum(ordered)
    if total <= 0:
        return None
    return round(sum(ordered[:count]) / total, 3)


def group_stats(samples, horizon: int, key_fn) -> dict:
    buckets: dict[str, list] = defaultdict(list)
    for row in execution(samples, horizon):
        key = key_fn(row)
        if key is None:
            continue
        buckets[str(key)].append(row)
    out = {}
    for key, rows in buckets.items():
        values = [row.gross for row in rows]
        excess = [row.excess for row in rows if row.excess is not None]
        out[key] = {
            "samples": len(rows),
            "mean": round(float(np.mean(values)), 2),
            "excess_mean": round(float(np.mean(excess)), 2) if excess else None,
            "win_pct": round(float(np.mean([1 if value > 0 else 0 for value in values]) * 100), 1),
        }
    return out


# ---------------------------------------------------------------- 因子分析


FACTORS = (
    "rps20",
    "rps60",
    "rps120",
    "ret20",
    "ret60",
    "slope20",
    "distance_ma20",
    "amount_ratio20",
    "convergence",
    "trend_days",
    "sector_score",
)


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index
        while end + 1 < len(order) and values[order[end + 1]] == values[order[index]]:
            end += 1
        score = (index + end) / 2
        for position in range(index, end + 1):
            ranks[order[position]] = score
        index = end + 1
    return ranks


def _corr(left: list[float], right: list[float]) -> float | None:
    if len(left) < 10:
        return None
    a = np.asarray(left, dtype=float)
    b = np.asarray(right, dtype=float)
    if a.std() <= 0 or b.std() <= 0:
        return None
    return round(float(np.corrcoef(a, b)[0, 1]), 3)


def factor_profile(records: list[dict], outcome: dict, factor: str, horizon: int) -> dict:
    pairs = []
    for record in records:
        key = (record["day"], record["code"])
        sample = outcome.get(key)
        if sample is None or sample.get("excess") is None:
            continue
        value = (record.get("features") or {}).get(factor)
        if value is None or not isinstance(value, (int, float)):
            continue
        pairs.append((float(value), float(sample["excess"]), float(sample["gross"])))
    if len(pairs) < 30:
        return {"samples": len(pairs), "status": "UNKNOWN"}
    values = [item[0] for item in pairs]
    excess = [item[1] for item in pairs]
    rank_ic = _corr(_rank(values), _rank(excess))
    order = sorted(range(len(pairs)), key=lambda index: values[index])
    deciles = []
    size = len(order)
    for bucket in range(10):
        start = int(size * bucket / 10)
        end = int(size * (bucket + 1) / 10)
        chunk = [pairs[index] for index in order[start:end]]
        if not chunk:
            continue
        deciles.append(
            {
                "bucket": bucket + 1,
                "n": len(chunk),
                "mean_excess": round(float(np.mean([item[1] for item in chunk])), 2),
                "mean_gross": round(float(np.mean([item[2] for item in chunk])), 2),
                "win_pct": round(float(np.mean([1 if item[2] > 0 else 0 for item in chunk]) * 100), 1),
            }
        )
    monotonic = None
    if len(deciles) >= 3:
        means = [item["mean_excess"] for item in deciles]
        monotonic = round(_corr(list(range(len(means))), means) or 0.0, 2)
    return {
        "samples": len(pairs),
        "rank_ic": rank_ic,
        "monotonicity": monotonic,
        "deciles": deciles,
    }


def _score_without(parts: dict, config, drop: str, rps_points: float) -> float:
    weights = te_config.effective_weights(config.base_weights, rps_points, 0.0)
    weights.pop(drop, None)
    total_weight = sum(weights.values())
    if total_weight <= 0:
        return 0.0
    total = 0.0
    for key, weight in weights.items():
        value = parts.get(key)
        if value is None:
            continue
        total += max(0.0, min(1.0, float(value))) * weight
    return round(total / total_weight * 100, 1)


def run_variant(
    session, matrix, benchmark, records, model, config, label, score_fn, top=20
) -> dict:
    grouped: dict[date, dict[str, float]] = defaultdict(dict)
    for record in records:
        score = score_fn(record)
        code = record["code"]
        day = record["day"]
        if code not in grouped[day] or score > grouped[day][code]:
            grouped[day][code] = score
    signals = []
    for day in sorted(grouped):
        picked = sorted(grouped[day].items(), key=lambda item: (-item[1], item[0]))[:top]
        for code, score in picked:
            signals.append(rb.Signal(code=code, signal_date=day, meta={"score": score}))
    result = rb.run_signals(
        session,
        signals,
        strategy=label,
        params={"top": top, "variant": label},
        horizons=HORIZONS,
        entry_mode="next_open",
        cost_bps=15.0,
        matrix=matrix,
        benchmark=benchmark,
    )
    return {"signals": len(signals), **stats(result.samples, 20)}


def worst_best_profile(records: list[dict], outcome: dict, horizon: int = 20) -> dict:
    rows = []
    for record in records:
        sample = outcome.get((record["day"], record["code"]))
        if sample is None or sample.get("gross") is None:
            continue
        rows.append((sample["gross"], record.get("features") or {}))
    if len(rows) < 40:
        return {"status": "UNKNOWN"}
    rows.sort(key=lambda item: item[0])
    size = len(rows)
    bottom = rows[: size // 4]
    top = rows[-size // 4 :]
    divergences = []
    for key in FACTORS + ("platform_width", "breakout_pct", "pullback_depth", "pullback_atr"):
        left = [item[1].get(key) for item in bottom if isinstance(item[1].get(key), (int, float))]
        right = [item[1].get(key) for item in top if isinstance(item[1].get(key), (int, float))]
        if len(left) < 10 or len(right) < 10:
            continue
        mean_left, mean_right = float(np.mean(left)), float(np.mean(right))
        pooled = float(np.std(left + right))
        effect = None if pooled == 0 else round((mean_right - mean_left) / pooled, 2)
        divergences.append(
            {
                "factor": key,
                "bottom_mean": round(mean_left, 3),
                "top_mean": round(mean_right, 3),
                "standardized_diff": effect,
            }
        )
    divergences.sort(key=lambda item: abs(item["standardized_diff"] or 0), reverse=True)
    return {
        "quartile_size": size // 4,
        "features": divergences,
        "bottom_mean_gross": round(float(np.mean([item[0] for item in bottom])), 2),
        "top_mean_gross": round(float(np.mean([item[0] for item in top])), 2),
    }


def mv_bucket(value: float | None) -> str | None:
    if value is None:
        return None
    if value < 5e9:
        return "<50亿"
    if value < 1e10:
        return "50-100亿"
    if value < 3e10:
        return "100-300亿"
    if value < 1e11:
        return "300-1000亿"
    return ">1000亿"


def trend_phase(value: int | None) -> str | None:
    if value is None:
        return None
    if value <= 5:
        return "0-5日"
    if value <= 20:
        return "6-20日"
    if value <= 60:
        return "21-60日"
    return ">60日"


def build_outcome_map(samples, horizon: int) -> dict:
    return {
        (row.signal_date, row.code): {
            "gross": row.gross,
            "excess": row.excess,
            "mfe": row.mfe,
            "mae": row.mae,
            "entry_date": str(row.entry_date),
            "exit_date": str(row.exit_date),
        }
        for row in execution(samples, horizon)
    }


# ---------------------------------------------------------------- 主流程


def main() -> int:
    parser = argparse.ArgumentParser(description="M6.1 模型失效归因（只读）")
    parser.add_argument("--db", default="backend/data/b1_research.db")
    parser.add_argument("--days", type=int, default=240)
    parser.add_argument("--report-dir", default="reports/trend_engine/m61")
    parser.add_argument("--cache", default="backend/data/m61_cache/attribution.json")
    parser.add_argument("--reuse-cache", action="store_true")
    parser.add_argument("--sha256", default="UNKNOWN")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    session = bt.open_readonly(db_path)
    payload = load_or_scan(
        session, db_path, args.days, Path(args.cache), args.reuse_cache
    )
    records = restore_records(payload)

    db_end = session.scalar(select(StockDaily.trade_date).order_by(StockDaily.trade_date.desc()).limit(1))
    calendar = (
        list(
            session.scalars(
                select(TradeCalendar.trade_date).where(TradeCalendar.trade_date <= db_end)
            )
        )
        if bt.has_table(session, "trade_calendar")
        else sorted(set(session.scalars(select(StockDaily.trade_date))))
    )
    matrix_start = calendar[max(0, len(calendar) - args.days - 140)]
    matrix = rb.load_matrix(session, matrix_start, db_end)
    benchmark = rb.Benchmark(session, matrix, "market")

    config_a = te_config.ModelAConfig()
    config_b = te_config.ModelBConfig()
    report: dict = {
        "db": str(db_path),
        "sha256": args.sha256,
        "scan_start": payload["scan_start"],
        "scan_end": payload["scan_end"],
        "days": payload["days"],
        "counts": {model: len(items) for model, items in records.items()},
        "models": {},
        "multiple_testing": {},
    }

    tested_variants = 0
    for model, config in (("A", config_a), ("B", config_b)):
        items = records[model]
        if not items:
            continue
        result, signals = run_all(session, matrix, benchmark, items, model, config)
        outcome = build_outcome_map(result.samples, 20)
        model_node = {
            "signals": len(signals),
            "overall": {str(h): stats(result.samples, h) for h in HORIZONS},
        }

        # 环境 / 行业 / 市值 / 趋势阶段：直接按记录特征分组（用 outcome 映射）
        by_regime = defaultdict(list)
        by_industry = defaultdict(list)
        by_mv = defaultdict(list)
        by_phase = defaultdict(list)
        for record in items:
            sample = outcome.get((record["day"], record["code"]))
            if sample is None:
                continue
            by_regime[record.get("regime") or "UNKNOWN"].append(sample)
            by_industry[(record.get("industry") or "UNKNOWN").split("-")[0]].append(sample)
            by_mv[mv_bucket(record.get("total_mv")) or "UNKNOWN"].append(sample)
            by_phase[trend_phase((record.get("features") or {}).get("trend_days")) or "UNKNOWN"].append(sample)

        def summarize_buckets(buckets: dict) -> dict:
            out = {}
            for key, rows in buckets.items():
                values = [row["gross"] for row in rows if row["gross"] is not None]
                excess = [row["excess"] for row in rows if row["excess"] is not None]
                if not values:
                    continue
                out[key] = {
                    "samples": len(values),
                    "mean": round(float(np.mean(values)), 2),
                    "excess_mean": round(float(np.mean(excess)), 2) if excess else None,
                    "win_pct": round(float(np.mean([1 if value > 0 else 0 for value in values]) * 100), 1),
                }
            return out

        model_node["buckets"] = {
            "regime": summarize_buckets(by_regime),
            "industry": dict(
                sorted(summarize_buckets(by_industry).items(), key=lambda item: -item[1]["samples"])[:15]
            ),
            "market_cap": summarize_buckets(by_mv),
            "trend_phase": summarize_buckets(by_phase),
        }

        # 因子 IC / 十分位
        model_node["factors"] = {
            factor: factor_profile(items, outcome, factor, 20) for factor in FACTORS
        }

        # 去一因子消融
        ablations = {}
        for drop in ("structure", "breakout", "volume", "pullback", "rps"):
            if drop not in (config.base_weights and dict(config.base_weights)) and drop != "rps":
                continue
            ablations[f"drop_{drop}"] = run_variant(
                session,
                matrix,
                benchmark,
                items,
                model,
                config,
                f"{model}-drop-{drop}",
                lambda record, drop=drop: _score_without(
                    record["parts"], config, drop, config.rps_points
                ),
            )
            tested_variants += 1
        model_node["ablations"] = ablations

        # 单因子选股（只做单日 top20，用于方向对照）
        single = {}
        for factor in ("rps20", "rps60", "ret20", "slope20", "amount_ratio20", "trend_days"):
            single[factor] = run_variant(
                session,
                matrix,
                benchmark,
                items,
                model,
                config,
                f"{model}-single-{factor}",
                lambda record, factor=factor: float(
                    (record.get("features") or {}).get(factor) or -1e9
                ),
            )
            tested_variants += 1
        model_node["single_factor"] = single

        model_node["worst_vs_best"] = worst_best_profile(items, outcome)
        report["models"][model] = model_node

    # 多重假设检验：Bonferroni 阈值
    report["multiple_testing"] = {
        "variants_tested": tested_variants,
        "bonferroni_alpha": round(0.05 / max(tested_variants, 1), 5),
        "note": "所有变体的 20 日超额 t 检验 p 值需低于该阈值才算显著；本报告不据此调参",
    }

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "attribution.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    write_markdown(report_dir / "02-model-failure-attribution.md", report)
    print(f"归因完成：{report_dir / '02-model-failure-attribution.md'}")
    return 0


def write_markdown(path: Path, report: dict) -> None:
    lines = ["# M6.1-B Model A/B 失效归因\n"]
    lines.append(f"- 数据库：`{report['db']}`")
    lines.append(f"- SHA256：`{report['sha256']}`")
    lines.append(f"- 区间：{report['scan_start']} ~ {report['scan_end']}（{report['days']} 个交易日）")
    lines.append(f"- 合格信号：A {report['counts'].get('A')} / B {report['counts'].get('B')}")
    lines.append("- 成交口径：T+1 开盘、15 bps、停牌顺延、涨停不可买；全部合格信号按每日 top50 计算\n")

    for model, node in report["models"].items():
        lines.append(f"## Model {model}\n")
        lines.append("### 收益分布\n")
        lines.append("| 持有期 | 样本 | 均值 | 中位 | 胜率 | 超额 | PF | MFE | MAE | MFE/MAE | 偏度 | 前5%贡献 | 剔除前5%均值 |")
        lines.append("| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for horizon, stats in node["overall"].items():
            lines.append(
                f"| {horizon} | {stats.get('samples')} | {stats.get('mean')} | {stats.get('median')} | "
                f"{stats.get('win_pct')} | {stats.get('excess_mean')} | {stats.get('profit_factor')} | "
                f"{stats.get('mfe_mean')} | {stats.get('mae_mean')} | {stats.get('mfe_mae_ratio')} | "
                f"{stats.get('skew')} | {stats.get('top5_contribution')} | {stats.get('mean_excluding_top5pct')} |"
            )
        lines.append("")

        lines.append("### 分层（20 日超额）\n")
        for name in ("regime", "market_cap", "trend_phase"):
            lines.append(f"**{name}**")
            lines.append("| 组 | 样本 | 均值 | 超额 | 胜率 |")
            lines.append("| --- | ---: | ---: | ---: | ---: |")
            for key, item in node["buckets"][name].items():
                lines.append(
                    f"| {key} | {item['samples']} | {item['mean']} | {item['excess_mean']} | {item['win_pct']} |"
                )
            lines.append("")
        lines.append("**industry（前 10，按样本）**")
        lines.append("| 行业 | 样本 | 均值 | 超额 |")
        lines.append("| --- | ---: | ---: | ---: |")
        for key, item in list(node["buckets"]["industry"].items())[:10]:
            lines.append(f"| {key} | {item['samples']} | {item['mean']} | {item['excess_mean']} |")
        lines.append("")

        lines.append("### 因子 IC 与十分位（20 日超额）\n")
        lines.append("| 因子 | 样本 | Rank IC | 单调性 | D1 | D5 | D10 |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
        for factor, profile in node["factors"].items():
            if profile.get("status") == "UNKNOWN":
                lines.append(f"| {factor} | {profile.get('samples')} | — | — | — | — | — |")
                continue
            deciles = profile["deciles"]
            pick = lambda index: deciles[index]["mean_excess"] if len(deciles) > index else None
            lines.append(
                f"| {factor} | {profile['samples']} | {profile['rank_ic']} | {profile['monotonicity']} | "
                f"{pick(0)} | {pick(4)} | {pick(9)} |"
            )
        lines.append("")

        lines.append("### 去一因子 / 单因子（20 日超额）\n")
        lines.append("| 变体 | 信号 | 样本 | 均值 | 超额 | 胜率 |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
        for key, item in {**node["ablations"], **node["single_factor"]}.items():
            lines.append(
                f"| {key} | {item.get('signals')} | {item.get('samples')} | {item.get('mean')} | "
                f"{item.get('excess_mean')} | {item.get('win_pct')} |"
            )
        lines.append("")

        prof = node["worst_vs_best"]
        if prof.get("status") != "UNKNOWN":
            lines.append("### 最差 25% vs 最好 25% 的特征差异\n")
            lines.append("| 特征 | 最差组均值 | 最好组均值 | 标准化差 |")
            lines.append("| --- | ---: | ---: | ---: |")
            for item in prof["features"][:8]:
                lines.append(
                    f"| {item['factor']} | {item['bottom_mean']} | {item['top_mean']} | "
                    f"{item['standardized_diff']} |"
                )
            lines.append("")

    lines.append("## 多重检验\n")
    lines.append(f"- 变体数：{report['multiple_testing']['variants_tested']}")
    lines.append(f"- Bonferroni α：{report['multiple_testing']['bonferroni_alpha']}")
    lines.append(f"- {report['multiple_testing']['note']}\n")

    lines.append("## 结论要点\n")
    for model, node in report["models"].items():
        overall = node["overall"].get("20", {})
        top5 = overall.get("top5_contribution")
        rest = overall.get("mean_excluding_top5pct")
        lines.append(
            f"- Model {model}：20 日均值 {overall.get('mean')}，超额 {overall.get('excess_mean')}，"
            f"前 5% 贡献 {top5}，剔除后均值 {rest}；"
            f"MFE/MAE={overall.get('mfe_mae_ratio')}，偏度 {overall.get('skew')}"
        )
    lines.append(
        "- 若前 5% 贡献远大于 1 且剔除后均值为负，说明「抓到大牛股」来自极少数样本，"
        "没有形成稳定的整体优势；这也是固定阈值模型在该窗口失效的主因之一。"
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
