"""M6.2 Batch 4：交易级模拟器独立审计与账本重建（只读）。

内容：
1. 用 A2/B2 冻结信号重跑 A_fixed / E_combo（以及 A+B 组合），核对 M6.1 报告数字；
2. 逐日账本恒等式：`cash + market_value == equity`；
3. 交易级不变量：T+1、整手、成本、日期顺序、无负现金；
4. 收益来源拆解：暴露、交易数、持有期、换手、现金天数、尾部依赖。

产物：
- `reports/trend_engine/m62/trade_sim_audit.json`
- `reports/trend_engine/m62/trade_ledger_reconciliation.csv`
- `reports/trend_engine/m62/portfolio_daily_reconciliation.csv`

用法：

    python scripts/m62_trade_sim_audit.py
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
BACKEND = ROOT / "backend"
for path in (str(BACKEND), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import backtest_trend_engine as bt  # noqa: E402
import run_m61_experiments as exp  # noqa: E402
import run_m61_trade_sim as m61sim  # noqa: E402
from app.models import StockDaily, TradeCalendar  # noqa: E402
from app.services.trend_engine import trade_sim  # noqa: E402
from sqlalchemy import select  # noqa: E402

OUT = ROOT / "reports/trend_engine/m62"
M61_REPORTED = {
    ("A", "A_fixed"): {"total_return_pct": -19.87, "max_drawdown_pct": -25.57, "trades": 80, "win_pct": 37.5},
    ("A", "E_combo"): {"total_return_pct": -19.03, "max_drawdown_pct": -22.45, "trades": 131, "win_pct": 27.5},
    ("B", "A_fixed"): {"total_return_pct": -3.61, "max_drawdown_pct": -22.47, "trades": 95, "win_pct": 45.3},
    ("B", "E_combo"): {"total_return_pct": -0.15, "max_drawdown_pct": -24.50, "trades": 179, "win_pct": 25.7},
    ("A+B", "A_fixed"): {"total_return_pct": -12.34, "max_drawdown_pct": -24.67, "trades": 101, "win_pct": 52.5},
    ("A+B", "E_combo"): {"total_return_pct": 0.34, "max_drawdown_pct": -14.96, "trades": 196, "win_pct": 30.6},
}


def combined_signals(cache: dict, selections: dict, days: set[date]) -> list[trade_sim.Signal]:
    merged: dict[tuple[date, str], trade_sim.Signal] = {}
    for model in ("A", "B"):
        spec = exp.VARIANTS[model][selections[model]]
        for item in m61sim.build_signals(cache, model, spec, days):
            key = (item.day, item.code)
            if key not in merged or item.score > merged[key].score:
                merged[key] = item
    grouped: dict[date, list[trade_sim.Signal]] = defaultdict(list)
    for (day, _code), item in merged.items():
        grouped[day].append(item)
    out: list[trade_sim.Signal] = []
    for day, items in grouped.items():
        out.extend(sorted(items, key=lambda item: -item.score)[:20])
    return out


def check(result: trade_sim.SimResult, config: trade_sim.SimConfig) -> dict:
    gap = max((abs(cash + value - equity) for _day, cash, value, equity in result.ledger), default=0.0)
    negative_cash = sum(1 for _day, cash, _value, _equity in result.ledger if cash < -0.01)
    bad_lot = sum(1 for trade in result.trades if trade.shares % 100 != 0)
    bad_order = sum(1 for trade in result.trades if trade.exit_date <= trade.entry_date)
    missing_fee = sum(1 for trade in result.trades if config.commission_bps > 0 and trade.entry_fee <= 0)
    pnl_sum = round(sum(getattr(trade, "pnl_exact", trade.pnl) for trade in result.trades), 6)
    unrealized = round(
        sum(
            item.get("unrealized_pnl_exact", item["unrealized_pnl"])
            for item in result.open_positions
        ),
        6,
    )
    equity_change = round(result.ledger[-1][3] - config.initial_cash, 2) if result.ledger else 0.0
    final_value = result.ledger[-1][2] if result.ledger else 0.0
    accounting = round(equity_change - pnl_sum - unrealized, 2)
    return {
        "ledger_identity_max_gap": round(gap, 4),
        "negative_cash_days": negative_cash,
        "lot_violations": bad_lot,
        "date_order_violations": bad_order,
        "trades_missing_entry_fee": missing_fee,
        "closed_pnl_sum": pnl_sum,
        "open_positions": len(result.open_positions),
        "unrealized_pnl": unrealized,
        "equity_change": equity_change,
        "final_market_value": round(final_value, 2),
        "accounting_gap": accounting,
        "invariants_ok": gap < 0.02
        and negative_cash == 0
        and bad_lot == 0
        and bad_order == 0
        and missing_fee == 0
        and abs(accounting) <= 0.05,
    }


def decomposition(result: trade_sim.SimResult, config: trade_sim.SimConfig) -> dict:
    pnls = sorted((trade.pnl for trade in result.trades), reverse=True)
    total = sum(pnls)
    top = sum(pnls[:5])
    positives = sum(value for value in pnls if value > 0)
    zero_exposure_days = sum(1 for value in result.exposure if value <= 0.0001)
    return {
        "trades": len(result.trades),
        "avg_exposure": result.metrics.get("exposure"),
        "zero_position_days": zero_exposure_days,
        "total_days": len(result.exposure),
        "avg_hold_days": result.metrics.get("avg_hold_days"),
        "turnover": result.metrics.get("turnover"),
        "top5_pnl": round(top, 2),
        "top5_share_of_positive": round(top / positives, 4) if positives > 0 else None,
        "top5_share_of_abs_total": round(top / abs(total), 4) if total else None,
        "skipped": result.skipped,
        "final_equity": result.ledger[-1][3] if result.ledger else None,
    }


def main() -> int:
    cache = json.loads((ROOT / "backend/data/m61_cache/attribution.json").read_text(encoding="utf-8"))
    cache["records_parsed"] = {
        model: [{**item, "day": date.fromisoformat(item["day"])} for item in items]
        for model, items in cache["records"].items()
    }
    experiment = json.loads((ROOT / "reports/trend_engine/m61/experiment_results.json").read_text(encoding="utf-8"))
    selections = {
        model: (experiment["models"][model].get("selection") or {}).get("variant") for model in ("A", "B")
    }

    session = bt.open_readonly(ROOT / "backend/data/b1_research.db")
    try:
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
        sim_calendar = [day for day in calendar if scan_days[0] <= day <= db_end]

        signals = {
            model: m61sim.build_signals(
                cache, model, exp.VARIANTS[model][selections[model]], set(scan_days)
            )
            for model in ("A", "B")
        }
        signals["A+B"] = combined_signals(cache, selections, set(scan_days))

        codes = sorted({item.code for items in signals.values() for item in items})
        bars = m61sim.load_bars_safe(session, codes, warmup_start, db_end)
        names = {
            code: name
            for code, name in session.execute(
                select(StockDaily.code, StockDaily.name).where(
                    StockDaily.trade_date == db_end, StockDaily.code.in_(codes)
                )
            ).all()
        }

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "db": str(ROOT / "backend/data/b1_research.db"),
            "selections": selections,
            "price_basis": "stock_daily 原始收盘价（未复权）；与 trade_sim 文档要求的『前复权』不一致，见审计报告",
            "runs": {},
            "reconciliation": {},
            "price_basis_warning": "HIGH：未复权价格会在除权日造成虚假跳空，所有交易级结果标记 PENDING_REVIEW",
        }
        ledger_rows: list[dict] = []
        daily_rows: list[dict] = []
        for model, items in signals.items():
            payload["runs"][model] = {}
            for rule in ("A_fixed", "E_combo"):
                config = m61sim.CONFIGS[rule]
                result = trade_sim.simulate(
                    bars_by_code=bars, calendar=sim_calendar, signals=items, config=config, names=names
                )
                checks = check(result, config)
                payload["runs"][model][rule] = {
                    "metrics": result.metrics,
                    "checks": checks,
                    "decomposition": decomposition(result, config),
                }
                reported = M61_REPORTED.get((model, rule))
                if reported:
                    payload["reconciliation"][f"{model}:{rule}"] = {
                        "reported": reported,
                        "reproduced": {
                            "total_return_pct": result.metrics.get("total_return_pct"),
                            "max_drawdown_pct": result.metrics.get("max_drawdown_pct"),
                            "trades": result.metrics.get("trades"),
                            "win_pct": result.metrics.get("win_pct"),
                        },
                        "return_match": bool(
                            abs((result.metrics.get("total_return_pct") or 0) - reported["total_return_pct"])
                            <= 0.01
                        ),
                        "drawdown_match": bool(
                            abs((result.metrics.get("max_drawdown_pct") or 0) - reported["max_drawdown_pct"])
                            <= 0.01
                        ),
                    }
                cumulative = 0.0
                for trade in result.trades:
                    cumulative = round(cumulative + trade.pnl, 2)
                    ledger_rows.append(
                        {
                            "model": model,
                            "rule": rule,
                            "code": trade.code,
                            "entry_date": trade.entry_date,
                            "exit_date": trade.exit_date,
                            "entry_price": round(trade.entry_price, 4),
                            "exit_price": round(trade.exit_price, 4),
                            "shares": trade.shares,
                            "entry_fee": trade.entry_fee,
                            "pnl": trade.pnl,
                            "return_pct": trade.return_pct,
                            "hold_days": trade.hold_days,
                            "reason": trade.reason,
                            "cumulative_pnl": cumulative,
                        }
                    )
                previous = None
                for day, cash, value, equity in result.ledger:
                    daily_return = round(equity / previous - 1, 6) if previous else 0.0
                    daily_rows.append(
                        {
                            "model": model,
                            "rule": rule,
                            "trade_date": day,
                            "cash": cash,
                            "market_value": value,
                            "equity": equity,
                            "exposure": round(value / equity, 4) if equity else 0.0,
                            "daily_return": daily_return,
                        }
                    )
                    previous = equity

        with (OUT / "trade_ledger_reconciliation.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(ledger_rows[0].keys()))
            writer.writeheader()
            writer.writerows(ledger_rows)
        with (OUT / "portfolio_daily_reconciliation.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(daily_rows[0].keys()))
            writer.writeheader()
            writer.writerows(daily_rows)

        payload["summary"] = {
            "all_invariants_ok": all(
                run["checks"]["invariants_ok"] for node in payload["runs"].values() for run in node.values()
            ),
            "max_ledger_gap": max(
                run["checks"]["ledger_identity_max_gap"] for node in payload["runs"].values() for run in node.values()
            ),
            "all_reported_returns_match": all(
                item["return_match"] for item in payload["reconciliation"].values()
            ),
        }
        (OUT / "trade_sim_audit.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )
        print(json.dumps(payload["summary"], ensure_ascii=False))
        print(
            json.dumps(
                {key: value["return_match"] for key, value in payload["reconciliation"].items()}, ensure_ascii=False
            )
        )
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
