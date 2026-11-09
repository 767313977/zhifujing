"""B1.2 趋势模板验证：滚动/分段留出、稳定性、参数敏感性、风险与结论分类。

策略规则来自生产 `services/trend.py`（Minervini 模板）+ RPS 候选池，本脚本不重新拟合。
因此：

- 2026Q1 只能叫「历史留出」，**不能**叫严格样本外（无法证明规则在 2026Q1 前冻结）；
- 真正的稳定性证据来自「非重叠留出块 + 参数敏感性 + 日期聚类 bootstrap」；
- 不挑选最佳参数，所有变体全部报告。

运行：

    python scripts/validate_trend.py --db backend/data/b1_research.db \
        --start 2025-09-25 --end 2026-06-30 --report backend/data/b1_trend_validation.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np
from sqlalchemy import func, select

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
BACKEND = ROOT / "backend"
for path in (str(BACKEND), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="B1.2 趋势模板验证")
    parser.add_argument("--db", required=True)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2025, 9, 25))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 6, 30))
    parser.add_argument("--horizons", default="5,10,20,40,60")
    parser.add_argument("--top", type=int, default=200)
    parser.add_argument("--min-rs", type=float, default=70.0)
    parser.add_argument("--cost-bps", type=float, default=0.0)
    parser.add_argument("--bootstrap", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--report")
    return parser.parse_args()


def _horizons(text: str) -> tuple[int, ...]:
    return tuple(sorted({int(item) for item in text.split(",") if item.strip()}))


def _variant(signals, top: int, min_rs: float):
    """从 superset 里按日取 RPS 前 N，并过滤 min_rs（不重新跑形态）。"""
    grouped = defaultdict(list)
    for signal in signals:
        if float(signal.meta.get("rps", -1)) >= min_rs:
            grouped[signal.signal_date].append(signal)
    out = []
    for day in sorted(grouped):
        picked = sorted(grouped[day], key=lambda s: (-float(s.meta["rps"]), s.code))[:top]
        out.extend(picked)
    return out


def _exec(samples, horizon: int):
    return [
        sample
        for sample in samples
        if sample.horizon == horizon and sample.status == "ok" and sample.gross is not None
    ]


def _net_mean(samples, horizon: int):
    values = [
        sample.net
        for sample in samples
        if sample.horizon == horizon and sample.status == "ok" and sample.net is not None
    ]
    return round(float(np.mean(values)), 2) if values else None


def _summary(samples, horizon: int) -> dict:
    from app.services import research_backtest as rb

    subset = [sample for sample in samples if sample.horizon == horizon]
    stat = rb._aggregate(subset, horizon)
    values = [sample.gross for sample in _exec(samples, horizon)]
    if not values:
        return {"samples": 0}
    arr = np.array(values, dtype=float)
    return {
        "samples": len(values),
        "mean": round(float(arr.mean()), 2),
        "median": round(float(np.median(arr)), 2),
        "excess_mean": stat.excess_mean,
        "excess_median": stat.excess_median,
        "beat_pct": stat.beat_pct,
        "mae_mean": stat.mae_mean,
        "pos_pct": stat.pos_pct,
        "q05": round(float(np.percentile(arr, 5)), 2),
        "q25": round(float(np.percentile(arr, 25)), 2),
        "q75": round(float(np.percentile(arr, 75)), 2),
        "q95": round(float(np.percentile(arr, 95)), 2),
    }


def _group(samples, horizon: int, key_fn) -> dict:
    buckets = defaultdict(list)
    for sample in samples:
        key = key_fn(sample)
        if key is not None:
            buckets[key].append(sample)
    out = {}
    for key, items in sorted(buckets.items(), key=lambda item: str(item[0])):
        if len(items) < 30:
            continue
        out[str(key)] = _summary(items, horizon)
    return out


def _bootstrap_by_date(samples, horizon: int, iterations: int, seed: int) -> dict:
    """按信号日聚类 bootstrap：先算每日均值，再对日期有放回重采样。"""
    groups = defaultdict(list)
    for sample in _exec(samples, horizon):
        groups[sample.signal_date].append(sample.gross)
    dates = sorted(groups)
    if len(dates) < 5:
        return {"dates": len(dates), "note": "日期太少，无法 bootstrap"}
    rng = random.Random(seed)
    means = []
    excess_means = []
    for _ in range(iterations):
        picked = [rng.choice(dates) for _ in range(len(dates))]
        values = [value for day in picked for value in groups[day]]
        means.append(float(np.mean(values)))
    # 超额用样本自带的 excess（同一日期内也可聚类）
    by_date_excess = defaultdict(list)
    for sample in _exec(samples, horizon):
        if sample.excess is not None:
            by_date_excess[sample.signal_date].append(sample.excess)
    excess_dates = sorted(by_date_excess)
    for _ in range(iterations):
        picked = [rng.choice(excess_dates) for _ in range(len(excess_dates))]
        values = [value for day in picked for value in by_date_excess[day]]
        excess_means.append(float(np.mean(values))) if values else None
    return {
        "dates": len(dates),
        "iterations": iterations,
        "mean_ci95": [round(float(np.percentile(means, 2.5)), 2), round(float(np.percentile(means, 97.5)), 2)],
        "excess_ci95": [
            round(float(np.percentile(excess_means, 2.5)), 2),
            round(float(np.percentile(excess_means, 97.5)), 2),
        ]
        if excess_means
        else None,
    }


def _extremes(samples, horizon: int) -> dict:
    values = np.array([sample.gross for sample in _exec(samples, horizon)], dtype=float)
    if not len(values):
        return {"samples": 0}
    q95 = float(np.percentile(values, 95))
    q99 = float(np.percentile(values, 99))
    total = float(values.sum())
    top5 = float(values[values >= q95].sum())
    top1 = float(values[values >= q99].sum())
    return {
        "samples": int(len(values)),
        "top5_threshold": round(q95, 2),
        "top1_threshold": round(q99, 2),
        "top5_sum_share_of_total": round(top5 / total, 3) if total > 0 else None,
        "top1_sum_share_of_total": round(top1 / total, 3) if total > 0 else None,
        "trimmed_mean_ex_top5": round(float(values[values < q95].mean()), 2),
        "loss_le_10_pct": round(float((values <= -10).mean() * 100), 1),
        "gain_ge_10_pct": round(float((values >= 10).mean() * 100), 1),
    }


def _industry_excess_stats(samples, horizon: int, industry_returns: dict) -> dict:
    """个股收益 − 所属行业等权收益；行业归属缺失的样本不计入。"""
    selected = [
        sample
        for sample in _exec(samples, horizon)
        if industry_returns.get((sample.code, sample.signal_date, horizon)) is not None
    ]
    if not selected:
        return {"samples": 0, "mean": None, "median": None, "beat_pct": None}
    values = np.array(
        [
            sample.gross - industry_returns[(sample.code, sample.signal_date, horizon)]
            for sample in selected
        ],
        dtype=float,
    )
    return {
        "samples": len(selected),
        "mean": round(float(values.mean()), 2),
        "median": round(float(np.median(values)), 2),
        "beat_pct": round(float((values > 0).mean() * 100), 1),
    }


def _industry_map(session, codes):
    from sqlalchemy import select

    from app.models import StockBasic

    rows = session.execute(
        select(StockBasic.code, StockBasic.industry, StockBasic.free_float_shares).where(
            StockBasic.code.in_(codes)
        )
    ).all()
    return {code: (industry, shares) for code, industry, shares in rows}


def _signal_env(session, matrix, signal):
    """信号日的行业、流动性（20 日均额）、市值代理。"""
    info = signal.meta.get("_info", {})
    pos = matrix.date_pos.get(signal.signal_date)
    col = matrix.code_pos.get(signal.code)
    if pos is None or col is None:
        return info
    close = matrix.close[pos, col]
    amounts = matrix.amount[max(0, pos - 20) : pos, col]
    amounts = amounts[np.isfinite(amounts)]
    liquidity = float(amounts.mean()) if len(amounts) else None
    shares = info.get("shares")
    mv = float(close) * float(shares) if shares and np.isfinite(close) else None
    return {**info, "close": float(close) if np.isfinite(close) else None, "liquidity": liquidity, "mv": mv}


def _benchmark_return(matrix, codes: set[str], entry_day: date | None, exit_day: date | None) -> float | None:
    """一组股票（同行业）的等权收益，口径与全市场基准一致。"""
    if not entry_day or not exit_day:
        return None
    from app.services import research_backtest as rb

    entry_pos = matrix.date_pos.get(entry_day)
    exit_pos = matrix.date_pos.get(exit_day)
    if entry_pos is None or exit_pos is None:
        return None
    values = []
    for code in codes:
        col = matrix.code_pos.get(code)
        if col is None:
            continue
        if not matrix.present[entry_pos, col] or not matrix.present[exit_pos, col]:
            continue
        open_ = matrix.open[entry_pos, col]
        close_e = matrix.close[entry_pos, col]
        pct_e = matrix.pct[entry_pos, col]
        if not np.isfinite(open_) or not np.isfinite(close_e) or open_ <= 0:
            continue
        if rb._entry_limit_up(
            code,
            matrix.name[entry_pos, col],
            entry_day,
            float(open_),
            float(close_e),
            None if not np.isfinite(pct_e) else float(pct_e),
        ):
            continue
        close_x = matrix.close[exit_pos, col]
        low_x = matrix.low[exit_pos, col]
        pct_x = matrix.pct[exit_pos, col]
        if not np.isfinite(close_x):
            continue
        if rb._exit_limit_down(
            code,
            matrix.name[exit_pos, col],
            exit_day,
            float(close_x),
            None if not np.isfinite(low_x) else float(low_x),
            None if not np.isfinite(pct_x) else float(pct_x),
        ):
            continue
        factor = 1 + (close_e / open_ - 1)
        for pos in range(entry_pos + 1, exit_pos + 1):
            if matrix.present[pos, col]:
                pct = matrix.pct[pos, col]
                if np.isfinite(pct):
                    factor *= 1 + pct / 100
        values.append((factor - 1) * 100)
    return float(np.mean(values)) if values else None


def main() -> int:
    args = _args()
    path = Path(args.db).resolve()
    if not path.exists():
        raise SystemExit(f"数据库不存在：{path}")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.environ["DB_PATH"] = str(path)

    from app.db import session_scope
    from app.models import StockDaily
    from app.services import research_backtest as rb

    horizons = _horizons(args.horizons)
    with session_scope() as session:
        logging.info("生成趋势模板 superset（top=300, min_rs=60）…")
        superset = rb.signals_from_trend_template(
            session, args.start, args.end, candidate_top=300, mode="rps60", min_rs=60.0
        )
        logging.info("superset 信号 %d 条", len(superset))
        codes = sorted({signal.code for signal in superset})
        info = _industry_map(session, codes)
        for signal in superset:
            industry, shares = info.get(signal.code, (None, None))
            signal.meta["_info"] = {"industry": industry, "shares": shares}

        # 行业匹配基准：取信号涉及的行业，加载这些行业的全部股票
        from app.models import StockBasic

        industries = {value[0] for value in info.values() if value[0]}
        industry_codes = defaultdict(set)
        if industries:
            for code, industry in session.execute(
                select(StockBasic.code, StockBasic.industry).where(
                    StockBasic.industry.in_(industries)
                )
            ):
                if industry:
                    industry_codes[industry].add(code)

        main_signals = _variant(superset, args.top, args.min_rs)
        data_end = session.scalar(select(func.max(StockDaily.trade_date)))
        matrix = rb.load_matrix(session, args.start, data_end)
        benchmark = rb.Benchmark(session, matrix, "market")

        def run(signals, cost_bps=0.0, hor=horizons):
            return rb.run_signals(
                session,
                signals,
                strategy="trend_template",
                params={"top": args.top, "min_rs": args.min_rs},
                horizons=hor,
                entry_mode="next_open",
                cost_bps=cost_bps,
                matrix=matrix,
                benchmark=benchmark,
            )

        logging.info("主策略回测：%d 条信号", len(main_signals))
        result0 = run(main_signals, 0.0)
        result15 = run(main_signals, 15.0)
        logging.info("行业匹配基准…")
        industry_returns = {}
        industry_cache = {}
        for sample in result0.samples:
            if sample.status != "ok" or not sample.entry_date or not sample.exit_date:
                continue
            industry = info.get(sample.code, (None, None))[0]
            if not industry:
                continue
            key = (industry, sample.entry_date, sample.exit_date)
            if key not in industry_cache:
                industry_cache[key] = _benchmark_return(
                    matrix, industry_codes.get(industry, set()), sample.entry_date, sample.exit_date
                )
            value = industry_cache[key]
            if value is not None:
                industry_returns[(sample.code, sample.signal_date, sample.horizon)] = value
        logging.info("参数敏感性…")
        variants = []
        for top, min_rs in ((100, 70), (200, 70), (300, 70), (200, 60), (200, 80)):
            signals = _variant(superset, top, min_rs)
            variant = run(signals, 0.0, (20, 40, 60))
            variants.append(
                {
                    "top": top,
                    "min_rs": min_rs,
                    "signals": len(signals),
                    "summary": {str(h): _summary(variant.samples, h) for h in (20, 40, 60)},
                }
            )

        regimes = rb._regime_map(session, args.start, args.end)
        info_cache = {}
        for signal in main_signals:
            info_cache[(signal.code, signal.signal_date)] = _signal_env(session, matrix, signal)

        enriched = []
        for sample in result0.samples:
            key = (sample.code, sample.signal_date)
            env = info_cache.get(key, {})
            enriched.append((sample, env, regimes.get(sample.signal_date, "未知")))

        def by_env(horizon):
            out = {"quarter": {}, "regime": {}, "industry": {}, "mv": {}, "liquidity": {}}
            for name in out:
                buckets = defaultdict(list)
                for sample, env, regime in enriched:
                    if sample.horizon != horizon:
                        continue
                    if name == "quarter":
                        key = f"{sample.signal_date.year}Q{(sample.signal_date.month - 1) // 3 + 1}"
                    elif name == "regime":
                        key = regime
                    elif name == "industry":
                        key = env.get("industry") or "未知"
                    elif name == "mv":
                        key = env.get("mv")
                    else:
                        key = env.get("liquidity")
                    if key is not None:
                        buckets[key].append(sample)
                if name in ("mv", "liquidity"):
                    keys = sorted(buckets)
                    if len(keys) >= 4:
                        order = sorted(keys)
                        q = np.quantile([k for k in order], [0.25, 0.5, 0.75])
                        relabel = defaultdict(list)
                        for key in order:
                            bucket = "Q1" if key <= q[0] else "Q2" if key <= q[1] else "Q3" if key <= q[2] else "Q4"
                            relabel[bucket].extend(buckets[key])
                        buckets = relabel
                out[name] = {
                    str(key): _summary(items, horizon)
                    for key, items in sorted(buckets.items(), key=lambda item: str(item[0]))
                    if len(items) >= 30
                }
            return out

        purged_blocks = {
            "2025-09-25~2025-10-31": (date(2025, 9, 25), date(2025, 10, 31)),
            "2026-02-01~2026-02-28": (date(2026, 2, 1), date(2026, 2, 28)),
            "2026-06-01~2026-06-30": (date(2026, 6, 1), date(2026, 6, 30)),
        }
        block_out = {}
        for label, (lo, hi) in purged_blocks.items():
            subset = [sample for sample in result0.samples if lo <= sample.signal_date <= hi]
            block_out[label] = {str(h): _summary(subset, h) for h in (20, 40, 60)}

        main_summary = {
            str(h): {
                **_summary(result0.samples, h),
                "net15_mean": _net_mean(result15.samples, h),
                "industry_excess": _industry_excess_stats(result0.samples, h, industry_returns),
                "counts": result0.horizons[h].counts,
            }
            for h in horizons
        }
        bootstrap = {str(h): _bootstrap_by_date(result0.samples, h, args.bootstrap, args.seed) for h in (40, 60)}
        extremes = {str(h): _extremes(result0.samples, h) for h in (40, 60)}
        stability = {str(h): by_env(h) for h in (40, 60)}

        # 结论分类
        s40 = main_summary.get("40", {})
        s60 = main_summary.get("60", {})
        block60 = [block_out[label].get("60", {}).get("excess_mean") for label in purged_blocks]
        sens60 = [item["summary"].get("60", {}).get("excess_mean") for item in variants]
        ci60 = (bootstrap.get("60") or {}).get("excess_ci95")
        sufficient = s60.get("samples", 0) >= 200 and len([x for x in block60 if x is not None]) >= 2
        if not sufficient:
            verdict = "数据不足，无法判断"
        elif (
            (s60.get("excess_mean") or -999) > 0
            and ci60
            and ci60[0] > 0
            and sum(1 for x in block60 if x and x > 0) >= 2
            and all((x or -999) > 0 for x in sens60 if x is not None)
        ):
            verdict = "存在可重复的正向统计证据（仍需前瞻验证，不等于未来收益保证）"
        elif (s60.get("excess_mean") or -999) > 0 or (s40.get("excess_mean") or -999) > 0:
            verdict = "有条件成立：仅在部分时间/环境/参数下表现较好"
        elif (s60.get("excess_mean") or 0) <= 0 and (s40.get("excess_mean") or 0) <= 0:
            verdict = "研究结果偏负面"
        else:
            verdict = "证据不足"

        payload = {
            "params": {
                "start": str(args.start),
                "end": str(args.end),
                "horizons": list(horizons),
                "top": args.top,
                "min_rs": args.min_rs,
                "entry": "next_open",
                "cost_bps": [0, 15],
                "bootstrap": args.bootstrap,
            },
            "data": {
                "db": str(path),
                "superset_signals": len(superset),
                "main_signals": len(main_signals),
                "data_end": str(data_end),
                "matrix_days": len(matrix.calendar),
                "matrix_codes": len(matrix.codes),
            },
            "main": main_summary,
            "industry_neutral": {
                str(h): main_summary[str(h)]["industry_excess"] for h in horizons
            },
            "industry_note": "行业归属用 StockBasic.industry 当前分类，不是历史时点行业；行业基准为同行业全股票等权，结果需按此局限解读。",
            "purged_blocks": block_out,
            "stability": stability,
            "sensitivity": variants,
            "bootstrap": bootstrap,
            "extremes": extremes,
            "coverage": result0.data_coverage,
            "warnings": result0.warnings,
            "verdict": verdict,
            "freeze_note": "策略规则来自生产 services/trend.py；无证据证明在 2026Q1 前冻结，2026Q1 只能标为历史留出，不是严格样本外。",
        }

    if args.report:
        Path(args.report).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
    print(json.dumps({k: payload[k] for k in ("params", "data", "main", "verdict")}, ensure_ascii=False, indent=2, default=str))
    print("留出块 60 日超额：", {k: v.get("60", {}).get("excess_mean") for k, v in block_out.items()})
    print("敏感性 60 日超额：", [(v["top"], v["min_rs"], v["summary"].get("60", {}).get("excess_mean")) for v in variants])
    print("60 日 bootstrap 超额 CI：", (bootstrap.get("60") or {}).get("excess_ci95"))
    if args.report:
        print(f"报告：{args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
