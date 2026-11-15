"""M6.1-E/F 交易级风险退出与组合对照（只读）。

对已冻结的变体，分别用 5 套退出/组合规则做**交易级**模拟：

| 代号 | 规则 |
| --- | --- |
| A | 固定持有 20 日 |
| B | ATR 初始止损 2× |
| C | MA20 趋势破坏 + 40 日结构低点 |
| D | 组合约束（最多 5 只、单股 15%） |
| E | ATR + 移动止损 + MA + 结构 + 放量滞涨（组合） |

另输出 A / B / A+B 的权益曲线相关性、回撤重叠与交易盈亏来源。

用法：

    python scripts/run_m61_trade_sim.py --db backend/data/b1_research.db \
        --cache backend/data/m61_cache/attribution.json \
        --experiment reports/trend_engine/m61/experiment_results.json
"""

from __future__ import annotations

import argparse
import json
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
import run_m61_experiments as exp  # noqa: E402
from app.models import StockDaily, TradeCalendar  # noqa: E402
from app.services.trend_engine import trade_sim  # noqa: E402


def load_bars_safe(session, codes: list[str], start: date, end: date) -> dict[str, dict[date, trade_sim.DailyBar]]:
    """按 800 只分批（SQLite 变量上限），只读。"""
    out: dict[str, dict[date, trade_sim.DailyBar]] = defaultdict(dict)
    for index in range(0, len(codes), 800):
        batch = codes[index : index + 800]
        rows = session.execute(
            select(
                StockDaily.code,
                StockDaily.trade_date,
                StockDaily.open,
                StockDaily.high,
                StockDaily.low,
                StockDaily.close,
                StockDaily.pct_chg,
                StockDaily.amount,
                StockDaily.volume,
            ).where(
                StockDaily.trade_date >= start,
                StockDaily.trade_date <= end,
                StockDaily.code.in_(batch),
            )
        ).all()
        for code, day, open_, high, low, close, pct, amount, volume in rows:
            if None in (open_, high, low, close):
                continue
            out[code][day] = trade_sim.DailyBar(
                open=float(open_),
                high=float(high),
                low=float(low),
                close=float(close),
                pct=None if pct is None else float(pct),
                amount=None if amount is None else float(amount),
                volume=None if volume is None else float(volume),
            )
    return dict(out)


CONFIGS = {
    "A_fixed": trade_sim.SimConfig(horizon=20, max_positions=10, position_pct=0.10),
    "B_atr": trade_sim.SimConfig(
        horizon=60, max_positions=10, position_pct=0.10, use_atr_stop=True, atr_stop_multiple=2.0
    ),
    "C_trend": trade_sim.SimConfig(
        horizon=60, max_positions=10, position_pct=0.10,
        use_ma_exit=True, ma_exit_window=20, use_structure_exit=True, structure_days=40,
    ),
    "D_portfolio": trade_sim.SimConfig(horizon=20, max_positions=5, position_pct=0.15),
    "E_combo": trade_sim.SimConfig(
        horizon=60, max_positions=8, position_pct=0.12,
        use_atr_stop=True, atr_stop_multiple=2.0, use_trailing=True, trailing_multiple=3.0,
        use_ma_exit=True, ma_exit_window=20, use_structure_exit=True, structure_days=40,
        use_stagnation_exit=True,
    ),
}


def build_signals(cache: dict, model: str, spec: dict, days: set[date]) -> list[trade_sim.Signal]:
    signals = exp.signals_for(cache["records_parsed"][model], model, spec, days)
    return [trade_sim.Signal(code=item.code, day=item.signal_date, score=float(item.meta["score"])) for item in signals]


def curve_metrics(result: trade_sim.SimResult) -> dict:
    return result.metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="M6.1 交易级风险退出（只读）")
    parser.add_argument("--db", default="backend/data/b1_research.db")
    parser.add_argument("--cache", default="backend/data/m61_cache/attribution.json")
    parser.add_argument("--experiment", default="reports/trend_engine/m61/experiment_results.json")
    parser.add_argument("--report-dir", default="reports/trend_engine/m61")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    cache = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    cache["records_parsed"] = {
        model: [{**item, "day": date.fromisoformat(item["day"])} for item in items]
        for model, items in cache["records"].items()
    }
    experiment = json.loads(Path(args.experiment).read_text(encoding="utf-8"))

    session = bt.open_readonly(db_path)
    db_end = session.scalar(select(StockDaily.trade_date).order_by(StockDaily.trade_date.desc()).limit(1))
    calendar = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date <= db_end)
            .order_by(TradeCalendar.trade_date)
        )
    )
    scan_days = sorted({record["day"] for items in cache["records_parsed"].values() for record in items})
    warmup_start = calendar[max(0, calendar.index(scan_days[0]) - 120)]
    # 交易模拟只在扫描区间内推进；用全量日历（1990 起）会把暴露与年化稀释成无意义的值
    sim_calendar = [day for day in calendar if scan_days[0] <= day <= db_end]

    selections = {}
    for model in ("A", "B"):
        node = (experiment.get("models") or {}).get(model) or {}
        selections[model] = (node.get("selection") or {}).get("variant") or ("A1" if model == "A" else "B1")

    signals_by_model = {}
    codes: set[str] = set()
    for model in ("A", "B"):
        spec = exp.VARIANTS[model][selections[model]]
        items = build_signals(cache, model, spec, set(scan_days))
        signals_by_model[model] = items
        codes.update(item.code for item in items)

    # 组合：同日同票取较高分，再取前 20
    combined = {}
    for item in signals_by_model["A"] + signals_by_model["B"]:
        key = (item.day, item.code)
        if key not in combined or item.score > combined[key].score:
            combined[key] = item
    grouped: dict[date, list] = defaultdict(list)
    for (day, _code), item in combined.items():
        grouped[day].append(item)
    combined_signals = []
    for day, items in grouped.items():
        combined_signals.extend(sorted(items, key=lambda item: -item.score)[:20])
    signals_by_model["A+B"] = combined_signals
    codes.update(item.code for item in combined_signals)

    bars = load_bars_safe(session, sorted(codes), warmup_start, db_end)
    names = {
        code: name
        for code, name in session.execute(
            select(StockDaily.code, StockDaily.name).where(
                StockDaily.trade_date == db_end, StockDaily.code.in_(sorted(codes))
            )
        ).all()
    }

    report = {
        "db": str(db_path),
        "selections": selections,
        "scan_start": str(scan_days[0]),
        "scan_end": str(scan_days[-1]),
        "models": {},
        "note": "交易级模拟：T 日收盘信号 → T+1 开盘买入；卖出决策收盘后、次日开盘执行；含涨跌停/停牌/成本/整手/仓位上限",
    }

    curves = {}
    for model, signals in signals_by_model.items():
        node = {}
        for name, config in CONFIGS.items():
            result = trade_sim.simulate(
                bars_by_code=bars,
                calendar=sim_calendar,
                signals=signals,
                config=config,
                names=names,
            )
            node[name] = result.metrics
            if name in ("A_fixed", "E_combo"):
                curves[f"{model}:{name}"] = [value for _day, value in result.equity]
        report["models"][model] = node

    # 相关性（A/B 固定持有与组合）
    corr = {}
    for left, right in (("A:A_fixed", "B:A_fixed"), ("A:E_combo", "B:E_combo")):
        a, b = curves.get(left), curves.get(right)
        if a and b and len(a) == len(b):
            arr_a, arr_b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
            returns_a = np.diff(arr_a) / arr_a[:-1]
            returns_b = np.diff(arr_b) / arr_b[:-1]
            corr[f"{left} vs {right}"] = round(float(np.corrcoef(returns_a, returns_b)[0, 1]), 3)
    report["correlation"] = corr

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "risk_exit_results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    write_risk(report_dir / "07-risk-exit-backtest.md", report)
    write_portfolio(report_dir / "08-portfolio-backtest.md", report)
    print(f"交易级模拟完成：{report_dir}")
    return 0


def write_risk(path: Path, report: dict) -> None:
    lines = ["# M6.1-E 交易级风险退出回测\n"]
    lines.append(f"- 数据库：`{report['db']}`")
    lines.append(f"- 冻结变体：{report['selections']}")
    lines.append(f"- 区间：{report['scan_start']} ~ {report['scan_end']}")
    lines.append(f"- 口径：{report['note']}\n")
    lines.append("| 模型 | 规则 | 总收益% | 年化% | 最大回撤% | 回撤天数 | 交易数 | 胜率 | PF | 换手 | 平均持有 | 暴露 |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for model, node in report["models"].items():
        for name, metrics in node.items():
            lines.append(
                f"| {model} | {name} | {metrics.get('total_return_pct')} | {metrics.get('annualized_pct')} | "
                f"{metrics.get('max_drawdown_pct')} | {metrics.get('max_drawdown_days')} | "
                f"{metrics.get('trades')} | {metrics.get('win_pct')} | {metrics.get('profit_factor')} | "
                f"{metrics.get('turnover')} | {metrics.get('avg_hold_days')} | {metrics.get('exposure')} |"
            )
    lines.append("")
    lines.append("## 说明\n")
    lines.append("- A_fixed 是「固定持有 20 日」，其余规则把最长持有放宽到 60 日，由退出信号决定离场。")
    lines.append("- 所有规则共用同一批信号；差异只来自退出/组合约束。")
    lines.append("- 交易级模拟的成本、涨跌停、停牌、整手与仓位约束都计入；结果不是「理论收益」。")
    lines.append("- 若 E_combo 的回撤显著低于 A_fixed 但收益也下降，只能说明「降风险」，不能说明「有 alpha」。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_portfolio(path: Path, report: dict) -> None:
    lines = ["# M6.1-F 双模型组合研究\n"]
    lines.append(f"- 冻结变体：{report['selections']}")
    lines.append(f"- 相关性（日收益）：{report['correlation']}\n")
    lines.append("## 组合结论规则\n")
    lines.append("- 只有 A、B 分别通过 M6.1 准入标准，才讨论组合；")
    lines.append("- 组合需在风险调整后收益（收益/最大回撤）上稳定优于单模型；")
    lines.append("- 若相关性高、回撤重叠大，组合不会降低风险，不应为了保留双模型而强行组合。\n")
    for model in ("A", "B", "A+B"):
        node = (report["models"].get(model) or {}).get("E_combo") or {}
        if not node:
            continue
        lines.append(
            f"- {model}（E_combo）：收益 {node.get('total_return_pct')}%，"
            f"最大回撤 {node.get('max_drawdown_pct')}%，交易 {node.get('trades')} 笔"
        )
    lines.append("")
    lines.append("> 组合层结论以 M6.1 总验收为准；单模型未通过时，本文件只作风险特征记录，不构成组合推荐。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
