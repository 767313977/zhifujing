"""M6.3 Batch 2：交易模拟器整改核验（只读）。

用与 M6.1 相同的 A2/B2 信号与参数，在修复后的模拟器上重跑：
- F3 长期停牌：不再静默假装可卖出，超时标记 `UNVALUABLE`；
- F4 仓位：改用上一交易日收盘权益，消除日内前视；
- F6 重复信号：`(code, signal_date, model_version)` 幂等；
- F7 日期口径：记录 signal/trigger/fill 与持有交易日数；
- 价格域：本次仍是 `RAW_APPROX`（库内无复权因子），结果一律 `PENDING_REVIEW`。

产物：
- `reports/trend_engine/m63/trade_sim_remediation.json`
- `reports/trend_engine/m63/trade_ledger_reconciliation.csv`
- `reports/trend_engine/m63/portfolio_daily_reconciliation.csv`
- `reports/trend_engine/m63/corporate_action_ledger_reconciliation.csv`

用法：

    python scripts/m63_trade_sim_remediation.py
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
BACKEND = ROOT / "backend"
for path in (str(BACKEND), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, str(path))

import backtest_trend_engine as bt  # noqa: E402
import m62_trade_sim_audit as audit62  # noqa: E402
import run_m61_experiments as exp  # noqa: E402
import run_m61_trade_sim as m61sim  # noqa: E402
from app.models import StockDaily, TradeCalendar  # noqa: E402
from app.services.trend_engine import trade_sim  # noqa: E402
from sqlalchemy import select  # noqa: E402

OUT = ROOT / "reports/trend_engine/m63"


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
            model: m61sim.build_signals(cache, model, exp.VARIANTS[model][selections[model]], set(scan_days))
            for model in ("A", "B")
        }
        signals["A+B"] = audit62.combined_signals(cache, selections, set(scan_days))
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
            "price_mode": "RAW_APPROX",
            "note": "库内无复权因子与公司行为数据；信号价域为 raw 近似，全部结果 PENDING_REVIEW；本文件不构成策略有效性结论。",
            "runs": {},
            "comparison_to_m61": {},
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
                checks = audit62.check(result, config)
                payload["runs"][model][rule] = {
                    "metrics": result.metrics,
                    "checks": checks,
                    "decomposition": audit62.decomposition(result, config),
                    "warnings": result.warnings[:10],
                    "status": result.metrics.get("status"),
                }
                reported = audit62.M61_REPORTED.get((model, rule))
                if reported:
                    reproduced = {
                        "total_return_pct": result.metrics.get("total_return_pct"),
                        "max_drawdown_pct": result.metrics.get("max_drawdown_pct"),
                        "trades": result.metrics.get("trades"),
                    }
                    payload["comparison_to_m61"][f"{model}:{rule}"] = {
                        "m61": reported,
                        "m63_fixed": reproduced,
                        "delta_return_pp": round(
                            (reproduced["total_return_pct"] or 0) - reported["total_return_pct"], 2
                        ),
                        "delta_drawdown_pp": round(
                            (reproduced["max_drawdown_pct"] or 0) - reported["max_drawdown_pct"], 2
                        ),
                        "delta_trades": (reproduced["trades"] or 0) - reported["trades"],
                        "cause": "F4 仓位改为上一交易日权益（不再使用当日收盘权益）",
                    }
                cumulative = 0.0
                for trade in result.trades:
                    cumulative = round(cumulative + trade.pnl, 2)
                    ledger_rows.append(
                        {
                            "model": model,
                            "rule": rule,
                            "code": trade.code,
                            "signal_date": trade.signal_date,
                            "entry_date": trade.entry_date,
                            "exit_trigger_date": trade.exit_trigger_date,
                            "exit_fill_date": trade.exit_fill_date,
                            "entry_price_raw": round(trade.entry_price, 4),
                            "exit_price_raw": round(trade.exit_price, 4),
                            "shares": trade.shares,
                            "entry_fee": trade.entry_fee,
                            "pnl": trade.pnl,
                            "return_pct": trade.return_pct,
                            "holding_trading_days": trade.holding_trading_days,
                            "reason": trade.reason,
                            "model_version": trade.model_version,
                            "corporate_action_status": "UNKNOWN_NO_EVENT_DATA",
                            "cumulative_pnl": cumulative,
                        }
                    )
                previous = None
                for day, cash, value, equity in result.ledger:
                    daily_rows.append(
                        {
                            "model": model,
                            "rule": rule,
                            "trade_date": day,
                            "cash": cash,
                            "market_value": value,
                            "equity": equity,
                            "exposure": round(value / equity, 4) if equity else 0.0,
                            "daily_return": round(equity / previous - 1, 6) if previous else 0.0,
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

        ca_rows = []
        for model, node in payload["runs"].items():
            for rule, run in node.items():
                ca_rows.append(
                    {
                        "model": model,
                        "rule": rule,
                        "events_in_window": 0,
                        "cash_dividend_entries": 0,
                        "share_change_entries": 0,
                        "status": "MISSING_CORPORATE_ACTION_DATA",
                        "note": "无分红/送转/配股/退市事件数据，账本不生成任何虚拟现金流或股份变化",
                    }
                )
        with (OUT / "corporate_action_ledger_reconciliation.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=list(ca_rows[0].keys()))
            writer.writeheader()
            writer.writerows(ca_rows)

        payload["summary"] = {
            "all_invariants_ok": all(
                run["checks"]["invariants_ok"] for node in payload["runs"].values() for run in node.values()
            ),
            "max_ledger_gap": max(
                run["checks"]["ledger_identity_max_gap"] for node in payload["runs"].values() for run in node.values()
            ),
            "unvaluable_runs": sum(
                1
                for node in payload["runs"].values()
                for run in node.values()
                if run["metrics"].get("unvaluable_positions")
            ),
            "all_pending_review": all(
                run["metrics"].get("status") == "PENDING_REVIEW"
                for node in payload["runs"].values()
                for run in node.values()
            ),
        }
        (OUT / "trade_sim_remediation.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )
        print(json.dumps(payload["summary"], ensure_ascii=False))
        print(
            json.dumps(
                {
                    key: {"d_ret": value["delta_return_pp"], "d_dd": value["delta_drawdown_pp"], "d_trades": value["delta_trades"]}
                    for key, value in payload["comparison_to_m61"].items()
                },
                ensure_ascii=False,
            )
        )
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
