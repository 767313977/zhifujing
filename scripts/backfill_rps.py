"""按交易日回补 `rps_daily`（20/50/60/120/250 日五档）。

零外部请求：只读 `stock_daily`、`trade_calendar`，本地复利 + 中位秩。
写入 `rps_daily` 的当天行（upsert），可重复执行。

## 为什么需要它

`rps_daily` 原来只在每天收盘尾部扫描一次，历史只有零星几天；而且历史上不同日期
可能是**不同版本的代码**写出来的（实测 2026-10-21 的 `ret50/rps50` 全空，
120/250 有值 —— 那是 50 日窗口上线前的旧版本写入的）。要做 5~60 日策略验证，
必须用同一版本代码把目标区间的每个交易日重算一遍。

## 安全约束

- **必须显式 `--db`**；默认拒绝写 `backend/data/fupan.db`（主库）。
- 目标日期必须同时是 `trade_calendar` 交易日、且当天 `stock_daily` 有行情。
- 覆盖率闸使用 `scan_rps.estimate_pool_count()`（目标日之前 20 个交易日行数中位数），
  不拿当前 `stock_universe` 去卡历史日期；这是估计口径，报告里会原样输出。
- `--max-days` 是硬上限，超过就报错，不做静默截断。

## 用法

    python scripts/backfill_rps.py --db backend/data/b0_trial.db --status
    python scripts/backfill_rps.py --db backend/data/b0_trial.db --days 3 --report rps.json
    python scripts/backfill_rps.py --db backend/data/b0_trial.db \
        --start 2025-01-02 --end 2025-01-31 --max-days 25
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
BACKEND = ROOT / "backend"
for path in (str(BACKEND), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

logger = logging.getLogger("backfill_rps")
MAIN_DB = (ROOT / "backend" / "data" / "fupan.db").resolve()


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回补 RPS 五档（B0 隔离版）")
    parser.add_argument("--db", required=True, help="目标 SQLite（默认拒绝主库）")
    parser.add_argument("--allow-main-db", action="store_true")
    parser.add_argument("--days", type=int, default=3, help="最近 N 个有行情的交易日（默认 3）")
    parser.add_argument("--max-days", type=int, default=5, help="本次最多处理多少个交易日（硬上限）")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--dry-run", action="store_true", help="只列出目标日期，不算不写")
    parser.add_argument("--status", action="store_true", help="只输出库内 RPS 覆盖，不计算")
    parser.add_argument("--report", help="结果 JSON 路径")
    return parser.parse_args()


def _status(path: Path) -> dict:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    cols = [row[1] for row in con.execute("PRAGMA table_info(rps_daily)")]
    rows = con.execute(
        "select count(*), min(trade_date), max(trade_date), count(distinct trade_date) from rps_daily"
    ).fetchone()
    per_day = [
        {
            "date": str(day),
            "rows": int(cnt),
            "ret20": int(r20),
            "ret50": int(r50),
            "ret60": int(r60),
            "ret120": int(r120),
            "ret250": int(r250),
        }
        for day, cnt, r20, r50, r60, r120, r250 in con.execute(
            "select trade_date, count(*), "
            "sum(case when ret20 is not null then 1 else 0 end), "
            "sum(case when ret50 is not null then 1 else 0 end), "
            "sum(case when ret60 is not null then 1 else 0 end), "
            "sum(case when ret120 is not null then 1 else 0 end), "
            "sum(case when ret250 is not null then 1 else 0 end) "
            "from rps_daily group by trade_date order by trade_date"
        )
    ]
    con.close()
    return {
        "db": str(path),
        "columns": cols,
        "rows": rows[0],
        "first": str(rows[1]) if rows[1] else None,
        "last": str(rows[2]) if rows[2] else None,
        "days": rows[3],
        "per_day": per_day,
    }


def _target_days(session, args) -> list[date]:
    from sqlalchemy import func, select

    from app.models import StockDaily, TradeCalendar

    end = args.end or session.scalar(
        select(func.max(StockDaily.trade_date)).where(StockDaily.pct_chg.is_not(None))
    )
    if end is None:
        return []
    data_days = set(
        session.scalars(
            select(StockDaily.trade_date).where(StockDaily.pct_chg.is_not(None)).distinct()
        )
    )
    stmt = select(TradeCalendar.trade_date).where(TradeCalendar.trade_date <= end)
    if args.start:
        stmt = stmt.where(TradeCalendar.trade_date >= args.start)
        days = sorted(day for day in session.scalars(stmt) if day in data_days)
    else:
        days = sorted(
            day
            for day in session.scalars(
                stmt.order_by(TradeCalendar.trade_date.desc()).limit(args.days * 3)
            )
            if day in data_days
        )[-args.days :]
    if len(days) > args.max_days:
        raise SystemExit(
            f"目标区间有 {len(days)} 个交易日，超过 --max-days={args.max_days}；"
            "请显式放大 --max-days（本轮不做静默截断）。"
        )
    return days


def main() -> int:
    args = _args()
    path = Path(args.db).resolve()
    if path == MAIN_DB and not args.allow_main_db:
        raise SystemExit("拒绝写主库 backend/data/fupan.db；请用隔离副本，或显式加 --allow-main-db（不推荐）")
    if not path.exists():
        raise SystemExit(f"数据库不存在：{path}")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.status:
        print(json.dumps(_status(path), ensure_ascii=False, indent=2))
        return 0

    os.environ["DB_PATH"] = str(path)
    from app.db import session_scope
    from app.jobs import scan_rps

    with session_scope() as session:
        days = _target_days(session, args)
    if not days:
        logger.warning("没有可用交易日")
        return 1
    if args.dry_run:
        print(json.dumps({"db": str(path), "days": [d.isoformat() for d in days]}, ensure_ascii=False, indent=2))
        return 0

    results = []
    for index, day in enumerate(days, 1):
        with session_scope() as session:
            pool = scan_rps.estimate_pool_count(session, day)
        result = scan_rps.scan(day, pool_count=pool, with_meta=True)
        result["day"] = day.isoformat()
        result["index"] = index
        result["total"] = len(days)
        results.append(result)
        logger.info(
            "[%d/%d] %s：写入 %s 行，pool=%s（%s），各窗口分母=%s%s",
            index,
            len(days),
            day,
            result.get("rows"),
            result.get("pool"),
            result.get("pool_basis"),
            result.get("denominators"),
            "，覆盖率不足跳过" if result.get("skipped") else "",
        )

    payload = {
        "db": str(path),
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "days": [d.isoformat() for d in days],
        "results": results,
        "total_rows": sum(int(item.get("rows") or 0) for item in results),
        "skipped": sum(1 for item in results if item.get("skipped")),
    }
    if args.report:
        Path(args.report).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
    print(
        json.dumps(
            {key: value for key, value in payload.items() if key != "results"},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
