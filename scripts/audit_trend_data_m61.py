"""M6.1-A 历史数据质量审计（只读）。

对个股 / 指数 / 板块 / 宽度 / RPS / 日历 / ST / 退市逐项输出：

- 最早/最晚日期、有效交易日、每日行数分布；
- 缺失交易日（相对交易日历或行情日期并集）；
- 重复主键组；
- 覆盖率（相对个股日线）；
- PIT 可用性（当时是否可知）；
- 来源/更新时间（能证明的才写，不能证明写 UNKNOWN）。

并额外重算最近 120 个交易日的市场宽度（上涨/下跌/平盘、站上 MA20/60），
与 `breadth_daily` 对照 —— **只输出建议值，不写库**。

用法：

    python scripts/audit_trend_data_m61.py --db backend/data/b1_research.db
    python scripts/audit_trend_data_m61.py --db backend/data/history.db \
        --report-dir reports/trend_engine/m61/history
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.models import (  # noqa: E402
    BreadthDaily,
    StockDaily,
    TradeCalendar,
)

#: 审计的物理表与主键
TABLES = {
    "stock_daily": ("trade_date", "code"),
    "index_daily": ("trade_date", "code"),
    "sector_daily": ("trade_date", "sector_code"),
    "sector_member": ("trade_date", "sector_code", "code"),
    "rps_daily": ("trade_date", "code"),
    "breadth_daily": ("trade_date",),
    "market_sentiment": ("trade_date",),
    "fear_greed_daily": ("trade_date",),
    "limit_pool": ("trade_date", "code", "pool_type"),
    "trade_calendar": ("trade_date",),
    "stock_basic": ("code",),
    "pattern_hit": ("trade_date", "code", "pattern"),
}


def open_readonly(path: Path) -> Session:
    engine = create_engine(f"sqlite:///file:{path.as_posix()}?mode=ro&uri=true", future=True)
    connection = engine.connect()
    connection.exec_driver_sql("PRAGMA query_only=ON")
    return Session(bind=connection)


def has_table(session: Session, name: str) -> bool:
    return bool(
        session.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"), {"n": name}
        ).first()
    )


def _daily_counts(session: Session, table: str) -> list[tuple[date, int]]:
    session.execute(
        text(f"SELECT MIN(trade_date), MAX(trade_date), COUNT(DISTINCT trade_date) FROM {table}")
    ).one()
    counts = session.execute(
        text(f"SELECT trade_date, COUNT(*) FROM {table} GROUP BY trade_date ORDER BY trade_date")
    ).all()
    return [(date.fromisoformat(str(day)), int(count)) for day, count in counts]


def _duplicate_groups(session: Session, table: str, keys: tuple[str, ...]) -> int:
    columns = ", ".join(keys)
    sql = (
        f"SELECT COUNT(*) FROM (SELECT {columns}, COUNT(*) c FROM {table} "
        f"GROUP BY {columns} HAVING c > 1)"
    )
    return int(session.execute(text(sql)).scalar() or 0)


def _calendar(session: Session, end: date) -> list[date]:
    if has_table(session, "trade_calendar"):
        days = list(
            session.scalars(
                select(TradeCalendar.trade_date)
                .where(TradeCalendar.trade_date <= end)
                .order_by(TradeCalendar.trade_date)
            )
        )
        if days:
            return days
    # 没有日历：用个股行情日期并集
    return sorted(
        set(session.scalars(select(StockDaily.trade_date).where(StockDaily.trade_date <= end)))
    )


def audit_table(
    session: Session,
    table: str,
    keys: tuple[str, ...],
    calendar: list[date],
    stock_days: dict[date, int],
    stock_last: date | None,
    sha256: str,
) -> dict:
    if not has_table(session, table):
        return {
            "table": table,
            "exists": False,
            "row_count": 0,
            "min_date": None,
            "max_date": None,
            "distinct_dates": 0,
            "median_rows_per_day": None,
            "max_rows_per_day": None,
            "duplicate_key_groups": None,
            "missing_trade_days": None,
            "coverage_vs_stock": None,
            "lag_vs_stock_days": None,
            "pit_status": "NO_DATA",
            "source": "UNKNOWN",
            "notes": "表不存在",
            "sha256": sha256,
        }

    row_count = int(session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)
    if "trade_date" not in keys:
        pit, notes, source = _pit(table, session, None, None, None, [])
        return {
            "table": table,
            "exists": True,
            "row_count": row_count,
            "min_date": None,
            "max_date": None,
            "distinct_dates": 0,
            "median_rows_per_day": None,
            "max_rows_per_day": None,
            "duplicate_key_groups": _duplicate_groups(session, table, keys),
            "missing_trade_days": None,
            "coverage_vs_stock": None,
            "lag_vs_stock_days": None,
            "pit_status": pit,
            "source": source,
            "notes": "；".join(notes),
            "sha256": sha256,
        }
    min_date, max_date = session.execute(
        text(f"SELECT MIN(trade_date), MAX(trade_date) FROM {table}")
    ).one()
    min_date = date.fromisoformat(str(min_date)) if min_date else None
    max_date = date.fromisoformat(str(max_date)) if max_date else None
    counts = _daily_counts(session, table) if min_date else []
    daily = [count for _day, count in counts]
    distinct = {day for day, _count in counts}

    # 缺失交易日：只在「该表自己的覆盖区间」内比较，避免拿未来日历强行补
    missing: list[date] = []
    if min_date and max_date and calendar:
        window = [day for day in calendar if min_date <= day <= max_date]
        missing = [day for day in window if day not in distinct]

    coverage = None
    if stock_days and counts:
        ratios = [
            count / stock_days[day]
            for day, count in counts
            if day in stock_days and stock_days[day] > 0
        ]
        coverage = round(float(np.mean(ratios)), 4) if ratios else None

    lag = None
    if max_date and stock_last:
        lag = len([day for day in calendar if max_date < day <= stock_last])

    pit, notes, source = _pit(table, session, min_date, max_date, lag, counts)
    return {
        "table": table,
        "exists": True,
        "row_count": row_count,
        "min_date": str(min_date) if min_date else None,
        "max_date": str(max_date) if max_date else None,
        "distinct_dates": len(distinct),
        "median_rows_per_day": int(statistics.median(daily)) if daily else None,
        "max_rows_per_day": max(daily) if daily else None,
        "duplicate_key_groups": _duplicate_groups(session, table, keys),
        "missing_trade_days": len(missing),
        "missing_days_sample": [str(day) for day in missing[:20]],
        "coverage_vs_stock": coverage,
        "lag_vs_stock_days": lag,
        "pit_status": pit,
        "source": source,
        "notes": "；".join(notes),
        "sha256": sha256,
    }


def _pit(
    table: str,
    session: Session,
    min_date: date | None,
    max_date: date | None,
    lag: int | None,
    counts: list[tuple[date, int]],
) -> tuple[str, list[str], str]:
    notes: list[str] = []
    if table == "stock_daily":
        notes.append("无首次入库时间，只能证明「D 日收盘后的日终事实」")
        return "CLOSE_ONLY", notes, "iFinD / 腾讯（按采集路径）"
    if table == "index_daily":
        notes.append("与盘后日线同源；当前库存在滞后，见 lag_vs_stock_days")
        return "CLOSE_ONLY_LAG", notes, "iFinD / 开盘红"
    if table == "sector_daily":
        notes.append("开盘红每日自算板块涨跌幅（成分当日生效）")
        return "CLOSE_ONLY_LAG", notes, "开盘红"
    if table == "sector_member":
        notes.append("当日快照；历史成分只能按 trade_date 使用，不能用最新成分倒推")
        return "SNAPSHOT_ONLY", notes, "开盘红"
    if table == "rps_daily":
        counts_by_day = {day: count for day, count in counts}
        if counts_by_day:
            median = statistics.median(counts_by_day.values())
            bad = [day for day, count in counts_by_day.items() if count < median * 0.8]
            if bad:
                notes.append(
                    f"{len(bad)} 天的行数低于中位数 80%（疑似混版本/残缺），"
                    f"如 {', '.join(str(day) for day in sorted(bad)[:5])}"
                )
        return "DERIVED_AFTER_CLOSE", notes, "scan_rps（本地计算）"
    if table == "breadth_daily":
        notes.append("由全市场日线反算；当前库可能为空")
        return "DERIVED_AFTER_CLOSE", notes, "collect_breadth（本地计算）"
    if table == "market_sentiment":
        notes.append("涨跌家数/涨停统计为日终事实；历史回补时部分字段留空")
        return "CLOSE_ONLY", notes, "开盘红 / akshare"
    if table == "fear_greed_daily":
        notes.append("原始指标 + 滚动分位；两融项可能沿用前一交易日（margin_date 标明）")
        return "CLOSE_ONLY", notes, "本地计算 + akshare"
    if table == "trade_calendar":
        notes.append("预置到年底（含未来日期），不能当数据边界")
        return "FUTURE_PRELOADED", notes, "akshare"
    if table == "stock_basic":
        notes.append("当前快照：industry / total_mv 不是历史值，不能用于历史分组")
        return "CURRENT_SNAPSHOT", notes, "iFinD 建池（7 天一次）"
    if table == "pattern_hit":
        notes.append("当日扫描写入；只覆盖扫描过的日期")
        return "CLOSE_ONLY", notes, "本地扫描"
    return "UNKNOWN", notes, "UNKNOWN"


def audit_st(session: Session) -> dict:
    if not has_table(session, "stock_daily"):
        return {"status": "NO_DATA"}
    rows = session.execute(
        text(
            "SELECT trade_date, COUNT(*) total,"
            " SUM(CASE WHEN UPPER(name) LIKE '%ST%' THEN 1 ELSE 0 END) st"
            " FROM stock_daily GROUP BY trade_date ORDER BY trade_date"
        )
    ).all()
    st_days = [(str(day), int(st), int(total)) for day, total, st in rows if total]
    changes = session.execute(
        text(
            "SELECT code, COUNT(DISTINCT CASE WHEN UPPER(name) LIKE '%ST%' THEN 1 ELSE 0 END)"
            " FROM stock_daily WHERE name IS NOT NULL GROUP BY code"
            " HAVING COUNT(DISTINCT CASE WHEN UPPER(name) LIKE '%ST%' THEN 1 ELSE 0 END) > 1"
        )
    ).all()
    return {
        "status": "OK",
        "day_count": len(st_days),
        "first": st_days[0] if st_days else None,
        "last": st_days[-1] if st_days else None,
        "codes_with_st_transition": len(changes),
        "pit_note": "使用 stock_daily.name 的当日值；同一天更名为 ST 的时点无法证明在收盘前已知",
    }


def audit_delisted(session: Session, calendar: list[date], stock_last: date | None) -> dict:
    if not has_table(session, "stock_daily") or stock_last is None:
        return {"status": "NO_DATA"}
    rows = session.execute(
        text("SELECT code, MIN(trade_date), MAX(trade_date) FROM stock_daily GROUP BY code")
    ).all()
    # 用交易日历（或行情日期并集）定位「30 个交易日之前」
    position = {day: index for index, day in enumerate(calendar)}
    last_pos = position.get(stock_last, len(calendar) - 1)
    threshold_pos = max(last_pos - 30, 0)
    threshold_day = calendar[threshold_pos] if calendar else stock_last - timedelta(days=45)
    stale = [
        (code, str(first), str(last))
        for code, first, last in rows
        if last is not None and str(last) < str(threshold_day)
    ]
    return {
        "status": "OK",
        "codes": len(rows),
        "stale_codes_30_trade_days": len(stale),
        "sample": stale[:20],
        "pit_note": "退市股无法从现有库确认；stale 只是「最后一行显著更早」的近似，不能等同于退市",
    }


def audit_rps(session: Session, calendar: list[date], stock_days: dict[date, int]) -> dict:
    if not has_table(session, "rps_daily"):
        return {"status": "NO_DATA"}
    rows = session.execute(
        text(
            "SELECT trade_date, COUNT(*),"
            " SUM(CASE WHEN ret20 IS NOT NULL THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN ret60 IS NOT NULL THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN ret120 IS NOT NULL THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN ret250 IS NOT NULL THEN 1 ELSE 0 END)"
            " FROM rps_daily GROUP BY trade_date ORDER BY trade_date"
        )
    ).all()
    per_day = {
        str(day): {
            "rows": int(total),
            "ret20": int(r20 or 0),
            "ret60": int(r60 or 0),
            "ret120": int(r120 or 0),
            "ret250": int(r250 or 0),
        }
        for day, total, r20, r60, r120, r250 in rows
    }
    days = sorted(date.fromisoformat(day) for day in per_day)
    orphans = [str(day) for day in days if day not in stock_days]
    missing = [str(day) for day in sorted(stock_days) if day not in set(days)]
    mixed = []
    for day in days:
        item = per_day[str(day)]
        values = [item["ret20"], item["ret60"], item["ret120"], item["ret250"]]
        if any(value == 0 for value in values):
            mixed.append({"trade_date": str(day), **item})
    return {
        "status": "OK",
        "days": len(days),
        "first": str(days[0]) if days else None,
        "last": str(days[-1]) if days else None,
        "orphan_rps_days": orphans,
        "stock_days_without_rps": len(missing),
        "stock_days_without_rps_sample": missing[:20],
        "mixed_version_days": mixed,
        "pit_note": "RPS 由 scan_rps 收盘后计算；混版本日禁止直接用于跨日研究",
    }


def audit_sector(session: Session) -> dict:
    if not has_table(session, "sector_daily"):
        return {"status": "NO_DATA"}
    rows = session.execute(
        text(
            "SELECT taxonomy, COUNT(*), COUNT(DISTINCT sector_code),"
            " MIN(trade_date), MAX(trade_date), COUNT(DISTINCT trade_date)"
            " FROM sector_daily GROUP BY taxonomy"
        )
    ).all()
    return {
        "status": "OK",
        "taxonomies": [
            {
                "taxonomy": taxonomy,
                "rows": int(total),
                "boards": int(boards),
                "first": str(first),
                "last": str(last),
                "days": int(days),
            }
            for taxonomy, total, boards, first, last, days in rows
        ],
        "up_down_columns_null": _null_share(session, "sector_daily", "up_count"),
    }


def _null_share(session: Session, table: str, column: str) -> float | None:
    total = int(session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)
    if not total:
        return None
    nulls = int(
        session.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL")).scalar() or 0
    )
    return round(nulls / total, 4)


def recompute_breadth(session: Session, calendar: list[date], days: int = 120) -> list[dict]:
    """只读重算最近 N 个交易日的宽度；不写库。按每只票一次遍历，避免 O(日×票×窗口)。"""
    if not has_table(session, "stock_daily") or not calendar:
        return []
    target_days = calendar[-days:]
    target_set = set(target_days)
    start = calendar[max(0, len(calendar) - days - 70)]
    rows = session.execute(
        text(
            "SELECT trade_date, code, close, pct_chg FROM stock_daily"
            " WHERE trade_date >= :start AND close IS NOT NULL"
            " ORDER BY code, trade_date"
        ),
        {"start": str(start)},
    ).all()
    series: dict[str, list[tuple[date, float, float | None]]] = defaultdict(list)
    for day, code, close, pct in rows:
        series[code].append((date.fromisoformat(str(day)), float(close), pct))

    counters = {
        day: {"up": 0, "down": 0, "flat": 0, "above20": 0, "base20": 0, "above60": 0, "base60": 0}
        for day in target_days
    }
    for _code, items in series.items():
        dates = [item[0] for item in items]
        closes = np.asarray([item[1] for item in items], dtype=float)
        pcts = [item[2] for item in items]
        ma20 = np.convolve(closes, np.ones(20) / 20, mode="valid")
        ma60 = np.convolve(closes, np.ones(60) / 60, mode="valid")
        for index, day in enumerate(dates):
            if day not in target_set:
                continue
            bucket = counters[day]
            pct = pcts[index]
            if pct is not None:
                if pct > 0:
                    bucket["up"] += 1
                elif pct < 0:
                    bucket["down"] += 1
                else:
                    bucket["flat"] += 1
            ma20_index = index - 19
            if ma20_index >= 0:
                bucket["base20"] += 1
                if closes[index] > ma20[ma20_index]:
                    bucket["above20"] += 1
            ma60_index = index - 59
            if ma60_index >= 0:
                bucket["base60"] += 1
                if closes[index] > ma60[ma60_index]:
                    bucket["above60"] += 1

    out: list[dict] = []
    for day in target_days:
        bucket = counters[day]
        out.append(
            {
                "trade_date": str(day),
                "up": bucket["up"],
                "down": bucket["down"],
                "flat": bucket["flat"],
                "above_ma20_pct": round(bucket["above20"] / bucket["base20"] * 100, 2)
                if bucket["base20"]
                else None,
                "above_ma60_pct": round(bucket["above60"] / bucket["base60"] * 100, 2)
                if bucket["base60"]
                else None,
                "universe20": bucket["base20"],
                "universe60": bucket["base60"],
            }
        )
    return out


def compare_breadth(session: Session, recomputed: list[dict]) -> dict:
    if not has_table(session, "breadth_daily"):
        return {"status": "NO_TABLE"}
    stored = {
        str(row.trade_date): row
        for row in session.scalars(select(BreadthDaily)).all()
    }
    diffs = []
    for item in recomputed:
        row = stored.get(item["trade_date"])
        if row is None:
            continue
        diffs.append(
            {
                "trade_date": item["trade_date"],
                "stored_above20": row.above_ma20,
                "recomputed_above20": item["above_ma20_pct"],
                "delta": None
                if row.above_ma20 is None or item["above_ma20_pct"] is None
                else round(item["above_ma20_pct"] - row.above_ma20, 2),
            }
        )
    return {"status": "OK", "stored_rows": len(stored), "compared": len(diffs), "sample": diffs[:10]}


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "table",
        "row_count",
        "min_date",
        "max_date",
        "distinct_dates",
        "median_rows_per_day",
        "max_rows_per_day",
        "duplicate_key_groups",
        "missing_trade_days",
        "coverage_vs_stock",
        "lag_vs_stock_days",
        "pit_status",
        "source",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in columns})


def write_markdown(path: Path, audit: dict) -> None:
    lines = ["# M6.1 数据质量审计\n"]
    lines.append(f"- 数据库：`{audit['db']}`")
    lines.append(f"- SHA256：`{audit['sha256']}`")
    lines.append(f"- 生成时间：{audit['generated_at']}")
    lines.append("- 访问方式：`mode=ro` + `PRAGMA query_only=ON`\n")

    lines.append("## 1. 表级覆盖\n")
    lines.append("| 表 | 行数 | 起 | 止 | 交易日 | 中位行/日 | 重复键组 | 缺失交易日 | 覆盖(相对个股) | 滞后 | PIT |")
    lines.append("| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
    for row in audit["tables"]:
        lines.append(
            f"| {row['table']} | {row['row_count']} | {row['min_date']} | {row['max_date']} | "
            f"{row['distinct_dates']} | {row['median_rows_per_day']} | {row['duplicate_key_groups']} | "
            f"{row['missing_trade_days']} | {row['coverage_vs_stock']} | {row['lag_vs_stock_days']} | "
            f"{row['pit_status']} |"
        )
    lines.append("")

    lines.append("## 2. 个股日线\n")
    stock = audit["stock"]
    lines.append(f"- 股票数：{stock['codes']}；≥250 根：{stock['codes_ge_250']}")
    lines.append(f"- 最早：{stock['first']}；最晚：{stock['last']}；交易日：{stock['days']}")
    lines.append(f"- 逐日行数：中位 {stock['median_rows']} / 最大 {stock['max_rows']}")
    lines.append(f"- 逐日 name 非空率：{stock['name_nonnull_ratio']}")
    lines.append("")

    lines.append("## 3. 指数与板块对齐\n")
    for row in audit["tables"]:
        if row["table"] in ("index_daily", "sector_daily", "sector_member"):
            lines.append(
                f"- {row['table']}：{row['min_date']} ~ {row['max_date']}，"
                f"比个股末日滞后 {row['lag_vs_stock_days']} 个交易日；{row['notes']}"
            )
    lines.append("")

    lines.append("## 4. RPS\n")
    rps = audit["rps"]
    if rps.get("status") == "OK":
        lines.append(f"- {rps['first']} ~ {rps['last']}，{rps['days']} 天")
        lines.append(f"- 孤儿 RPS 日（无个股日线）：{rps['orphan_rps_days'] or '无'}")
        lines.append(f"- 有个股日线但无 RPS 的天数：{rps['stock_days_without_rps']}")
        lines.append(f"- 混版本日（某窗口全空）：{len(rps['mixed_version_days'])} 天")
        for item in rps["mixed_version_days"][:10]:
            lines.append(f"  - {item['trade_date']}：{item}")
    else:
        lines.append("- 无 rps_daily 表")
    lines.append("")

    lines.append("## 5. 市场宽度重算（不写库）\n")
    breadth = audit["breadth"]
    lines.append(f"- 对照结果：{breadth['compare']}")
    lines.append("- 最近 5 个交易日建议值：")
    for item in breadth["recomputed"][-5:]:
        lines.append(
            f"  - {item['trade_date']} 上涨 {item['up']} / 下跌 {item['down']} / 平盘 {item['flat']}，"
            f"站上 MA20 {item['above_ma20_pct']}%，站上 MA60 {item['above_ma60_pct']}%"
        )
    lines.append("")

    lines.append("## 6. ST 与退市\n")
    st = audit["st"]
    delisted = audit["delisted"]
    lines.append(f"- ST：{st}")
    lines.append(f"- 退市近似：{delisted}")
    lines.append("")

    lines.append("## 7. 结论\n")
    for item in audit["conclusions"]:
        lines.append(f"- {item}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="M6.1 数据质量审计（只读）")
    parser.add_argument("--db", required=True)
    parser.add_argument("--report-dir", default="reports/trend_engine/m61")
    parser.add_argument("--sha256", default="UNKNOWN")
    args = parser.parse_args()

    path = Path(args.db).resolve()
    if not path.exists():
        raise SystemExit(f"数据库不存在：{path}")
    session = open_readonly(path)
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    stock_last = session.scalar(select(func.max(StockDaily.trade_date)))
    calendar = _calendar(session, stock_last) if stock_last else []
    stock_days = dict(_daily_counts(session, "stock_daily")) if has_table(session, "stock_daily") else {}
    rows = [
        audit_table(session, table, keys, calendar, stock_days, stock_last, args.sha256)
        for table, keys in TABLES.items()
    ]

    stock_codes = session.execute(
        text("SELECT code, COUNT(*) c FROM stock_daily GROUP BY code")
    ).all()
    name_ratio = None
    if has_table(session, "stock_daily"):
        total = int(session.execute(text("SELECT COUNT(*) FROM stock_daily")).scalar() or 0)
        named = int(
            session.execute(
                text("SELECT COUNT(*) FROM stock_daily WHERE name IS NOT NULL")
            ).scalar()
            or 0
        )
        name_ratio = round(named / total, 4) if total else None

    recomputed = recompute_breadth(session, calendar)
    audit = {
        "db": str(path),
        "sha256": args.sha256,
        "generated_at": date.today().isoformat(),
        "tables": rows,
        "stock": {
            "codes": len(stock_codes),
            "codes_ge_250": sum(1 for _code, count in stock_codes if count >= 250),
            "first": str(min(stock_days)) if stock_days else None,
            "last": str(max(stock_days)) if stock_days else None,
            "days": len(stock_days),
            "median_rows": int(statistics.median(stock_days.values())) if stock_days else None,
            "max_rows": max(stock_days.values()) if stock_days else None,
            "name_nonnull_ratio": name_ratio,
        },
        "rps": audit_rps(session, calendar, stock_days),
        "sector": audit_sector(session),
        "breadth": {
            "recomputed": recomputed,
            "compare": compare_breadth(session, recomputed),
        },
        "st": audit_st(session),
        "delisted": audit_delisted(session, calendar, stock_last),
    }

    conclusions: list[str] = []
    for row in rows:
        if row["table"] in ("index_daily", "sector_daily") and (row["lag_vs_stock_days"] or 0) > 0:
            conclusions.append(
                f"{row['table']} 比个股末日滞后 {row['lag_vs_stock_days']} 个交易日，"
                "涉及该区间的市场/板块判断必须返回 fallback，不得当作当日数据"
            )
    if audit["rps"].get("mixed_version_days"):
        conclusions.append("RPS 存在混版本日，跨日研究必须用 data_quality.rps_day_status 拒绝")
    if audit["breadth"]["compare"].get("stored_rows") == 0:
        conclusions.append("breadth_daily 为空：市场环境只能由指数口径降级，或先重算（需单独授权写库）")
    if audit["delisted"].get("stale_codes_30_trade_days") == 0:
        conclusions.append("未发现 30 个交易日以上的 stale 代码：若为 history.db，则确认无退市股（幸存者偏差）")
    else:
        conclusions.append(
            f"发现 {audit['delisted']['stale_codes_30_trade_days']} 只 stale 代码；只能近似缺口语义，不能证明退市"
        )
    conclusions.append("stock_basic.industry / total_mv 是当前快照，不能用于历史行业/市值分组（前视）")
    audit["conclusions"] = conclusions

    write_csv(report_dir / "data_coverage_matrix.csv", rows)
    write_markdown(report_dir / "01-data-quality-audit.md", audit)
    (report_dir / "data_audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    gap_lines = ["# 数据缺口报告\n", f"- 数据库：`{path}`", f"- SHA256：`{args.sha256}`\n"]
    gap_lines.append("| 表 | 缺失交易日 | 示例 | 滞后(交易日) | 覆盖 |")
    gap_lines.append("| --- | ---: | --- | ---: | ---: |")
    for row in rows:
        gap_lines.append(
            f"| {row['table']} | {row['missing_trade_days']} | "
            f"{', '.join(row.get('missing_days_sample') or []) or '—'} | "
            f"{row['lag_vs_stock_days']} | {row['coverage_vs_stock']} |"
        )
    gap_lines.append("")
    gap_lines.append("## 不可用字段\n")
    gap_lines.append("- `sector_daily.up_count/down_count`：开盘红未提供，恒为 NULL，板块广度改用 `sector_member`。")
    gap_lines.append("- `breadth_daily`：本库为空，环境判定只能降级为指数口径。")
    gap_lines.append("- 退市名单 / 历史 ST 状态表：不存在；只能用当日 `stock_daily.name` 与 stale 近似。")
    (report_dir / "data_gap_report.md").write_text("\n".join(gap_lines), encoding="utf-8")

    pit_lines = ["# PIT 完整性报告\n", f"- 数据库：`{path}`\n"]
    pit_lines.append("| 表 | PIT 状态 | 来源 | 允许用途 |")
    pit_lines.append("| --- | --- | --- | --- |")
    usage = {
        "stock_daily": "T 日收盘后因子与次日开盘成交；不得假设盘中已知",
        "index_daily": "市场环境（收盘后）",
        "sector_daily": "板块强度（收盘后）；滞后日期必须 fallback",
        "sector_member": "仅按当日快照使用，禁止用最新成分回看历史",
        "rps_daily": "横截面相对强度；混版本日拒绝",
        "breadth_daily": "市场宽度；为空时不得伪造",
        "trade_calendar": "仅作日期轴；未来日期不能当数据边界",
        "stock_basic": "仅当前展示；历史分组禁用 industry/total_mv",
        "pattern_hit": "只覆盖扫描日；不能当连续信号源",
    }
    for row in rows:
        pit_lines.append(
            f"| {row['table']} | {row['pit_status']} | {row['source']} | {usage.get(row['table'], '—')} |"
        )
    pit_lines.append("")
    pit_lines.append("## 已知 PIT 缺口\n")
    pit_lines.append("- 无 `created_at` / 首次入库时间：`stock_daily`、`index_daily` 只能证明日终事实。")
    pit_lines.append("- 历史行业与市值不可得：`stock_basic` 是当前快照。")
    pit_lines.append("- 历史 ST 以当日名称近似；更名当天的盘中可知性无法证明。")
    pit_lines.append("- 退市股无法识别：`history.db` 按当前代码池回补，0 只退市股。")
    (report_dir / "pit_integrity_report.md").write_text("\n".join(pit_lines), encoding="utf-8")

    print(f"审计完成：{report_dir}")
    for item in conclusions:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
