"""B0.1 历史数据完整性审计（只读）。

用法：

    python scripts/audit_research_data.py --db backend/data/fupan.db
    python scripts/audit_research_data.py --db backend/data/history.db --json out.json

审计口径：

- 只以 SQLite `mode=ro` 打开，**不写库、不建表、不修改任何数据**；
- 交易日以 `trade_calendar` 为准；没有日历的库（history.db）退化为日线自身的日期并集，
  并在输出里明确标注；
- 「完整日」判据：当天行数 ≥ 窗口内单日最大行数 × 50%（与 `pattern_track` 同一思路）；
- 历史 ST：用 `stock_daily.name` 的**当日值**判（逐日非空率 100%），不用最新名字；
- 退市：用「最后一个有行情的日期」显著早于全库最后日期来近似，**只是缺口提示**，
  不能证明某只票已退市。

输出为 Markdown（默认 stdout），可直接贴进审计报告。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


def _open_ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"数据库不存在：{path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _tables(con: sqlite3.Connection) -> list[str]:
    rows = con.execute("select name from sqlite_master where type='table'").fetchall()
    return sorted(name for (name,) in rows)


def _one(con: sqlite3.Connection, sql: str, params: tuple = ()) -> tuple:
    row = con.execute(sql, params).fetchone()
    return row if row is not None else tuple()


def _date_str(value) -> str | None:
    return str(value) if value is not None else None


def _grade(ratio: float | None, has_history: bool) -> str:
    if ratio is None:
        return "D"
    if ratio >= 0.95 and has_history:
        return "A"
    if ratio >= 0.7:
        return "B"
    if ratio > 0:
        return "C"
    return "D"


@dataclass
class TableAudit:
    name: str
    exists: bool
    rows: int | None = None
    first: str | None = None
    last: str | None = None
    days: int | None = None
    grade: str = "D"
    notes: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)


def audit_stock_daily(con: sqlite3.Connection, calendar: list[str], label: str) -> TableAudit:
    tables = _tables(con)
    if "stock_daily" not in tables:
        return TableAudit("stock_daily", False, notes=["表不存在"])
    rows, first, last, codes = _one(
        con,
        "select count(*), min(trade_date), max(trade_date), count(distinct code) from stock_daily",
    )
    days = [str(r[0]) for r in con.execute("select distinct trade_date from stock_daily order by trade_date")]
    # 交易日历是 1990 起的长表；只取「数据首日 ~ 数据末日」这一段当分母，
    # 否则 506 天的库会被算成覆盖率 5%，等级无意义。
    in_range = [d for d in calendar if str(first) <= d <= str(last)]
    per_day = {str(day): int(cnt) for day, cnt in con.execute(
        "select trade_date, count(*) from stock_daily group by trade_date"
    )}
    counts = list(per_day.values())
    max_count = max(counts) if counts else 0
    threshold = max_count * 0.5
    incomplete = sorted(day for day, cnt in per_day.items() if cnt < threshold)
    med = statistics.median(counts) if counts else 0

    min_date = str(first)
    # 真正「数据起点之后才出现」的代码（可能是次新，也可能是历史覆盖不全）
    after_start = _one(
        con,
        "select count(*) from (select code from stock_daily group by code "
        "having min(trade_date) > date(?, '+30 day'))",
        (min_date,),
    )[0]
    long_history = _one(
        con,
        "select count(*) from (select code from stock_daily group by code having count(*) >= 250)",
    )[0]
    st_rows = _one(
        con,
        "select count(*) from stock_daily where upper(name) like '%ST%'",
    )[0]
    st_names = _one(
        con,
        "select count(distinct name) from stock_daily where upper(name) like '%ST%'",
    )[0]
    name_changes = _one(
        con,
        "select count(*) from (select code from stock_daily group by code having count(distinct name) > 1)",
    )[0]
    # 最后一个有行情的日期距全库最后日期超过 20 个交易日的代码
    stale = _one(
        con,
        "select count(*) from (select code, max(trade_date) m from stock_daily group by code) "
        "where m < (select max(trade_date) from stock_daily) and "
        "julianday((select max(trade_date) from stock_daily)) - julianday(m) > 30",
    )[0]

    notes = []
    if incomplete:
        notes.append(f"不完整交易日 {len(incomplete)} 天（<50% 最大行数），最早 {incomplete[0]}，最近 {incomplete[-1]}")
    if after_start:
        notes.append(f"{after_start} 只代码在数据起点 30 天后才首次出现（次新或历史覆盖不全）")
    if st_rows:
        notes.append(f"ST 相关行 {st_rows} 行、{st_names} 个简称；逐日 name 可用于历史 ST 判定")
    if name_changes:
        notes.append(f"{name_changes} 只代码在库内改过简称（含 ST 摘帽/戴帽）")
    if stale:
        notes.append(f"{stale} 只代码最后一根日线比全库最后日期早 30 天以上（可能是退市/长期停牌/出池）")

    return TableAudit(
        "stock_daily",
        True,
        rows=rows,
        first=_date_str(first),
        last=_date_str(last),
        days=len(days),
        grade=_grade(len(days) / max(len(in_range), 1), len(days) >= 250),
        notes=notes,
        detail={
            "codes": codes,
            "median_rows_per_day": med,
            "max_rows_per_day": max_count,
            "incomplete_days": len(incomplete),
            "incomplete_samples": incomplete[:10],
            "codes_ge_250_rows": long_history,
            "codes_first_seen_after_30d": after_start,
            "st_row_count": st_rows,
            "st_name_count": st_names,
            "name_change_codes": name_changes,
            "stale_codes_30d": stale,
            "label": label,
        },
    )


def audit_sector_daily(con: sqlite3.Connection) -> TableAudit:
    tables = _tables(con)
    if "sector_daily" not in tables:
        return TableAudit("sector_daily", False, notes=["表不存在"])
    rows, first, last, days = _one(
        con,
        "select count(*), min(trade_date), max(trade_date), count(distinct trade_date) from sector_daily",
    )
    by_tax = con.execute(
        "select taxonomy, count(*) , count(distinct trade_date), count(distinct sector_code) "
        "from sector_daily group by taxonomy"
    ).fetchall()
    nulls = {}
    for col in ("strength", "pct_chg", "amount", "net_inflow", "member_count", "up_count", "down_count"):
        nulls[col] = _one(con, f"select sum(case when {col} is null then 1 else 0 end) from sector_daily")[0]
    name_changes = _one(
        con,
        "select count(*) from (select sector_code, taxonomy from sector_daily "
        "group by sector_code, taxonomy having count(distinct name) > 1)",
    )[0]
    notes = []
    if nulls.get("up_count") == rows:
        notes.append("up_count/down_count 全为空：板块内部广度不能直接取自本表")
    if nulls.get("net_inflow") and nulls["net_inflow"] > rows * 0.9:
        notes.append(f"net_inflow 缺失 {nulls['net_inflow']}/{rows}：板块资金流历史极短")
    if nulls.get("member_count") and nulls["member_count"] > rows * 0.9:
        notes.append(f"member_count 缺失 {nulls['member_count']}/{rows}")
    if name_changes:
        notes.append(f"{name_changes} 个板块在库内改过名称")
    return TableAudit(
        "sector_daily",
        True,
        rows=rows,
        first=_date_str(first),
        last=_date_str(last),
        days=days,
        grade=_grade(days / 250, days >= 120),
        notes=notes,
        detail={
            "taxonomies": [
                {"taxonomy": tax, "rows": cnt, "days": d, "codes": c}
                for tax, cnt, d, c in by_tax
            ],
            "null_counts": nulls,
            "name_change_boards": name_changes,
        },
    )


def audit_sector_member(con: sqlite3.Connection) -> TableAudit:
    tables = _tables(con)
    if "sector_member" not in tables:
        return TableAudit("sector_member", False, notes=["表不存在"])
    rows, first, last, days = _one(
        con,
        "select count(*), min(trade_date), max(trade_date), count(distinct trade_date) from sector_member",
    )
    per_day = con.execute(
        "select trade_date, count(distinct sector_code), count(*) from sector_member group by trade_date order by trade_date"
    ).fetchall()
    per_board = con.execute(
        "select sector_code, trade_date, count(*) c from sector_member group by sector_code, trade_date"
    ).fetchall()
    sizes = [int(c) for _s, _d, c in per_board]
    tiny = sum(1 for c in sizes if c <= 1)
    small = sum(1 for c in sizes if 2 <= c <= 5)
    notes = []
    if days and days < 20:
        notes.append(f"只有 {days} 个交易日有快照：不能据此做 20 日以上的历史广度")
    if tiny:
        notes.append(f"{tiny} 个「板块-日」只有 1 只成员，疑似不完整，不能标记为完整")
    return TableAudit(
        "sector_member",
        True,
        rows=rows,
        first=_date_str(first),
        last=_date_str(last),
        days=days,
        grade=_grade(days / 60, days >= 60),
        notes=notes,
        detail={
            "per_day": [
                {"date": _date_str(d), "boards": int(b), "rows": int(c)} for d, b, c in per_day
            ],
            "board_day_count": len(per_board),
            "member_size_median": statistics.median(sizes) if sizes else None,
            "board_day_size_le_1": tiny,
            "board_day_size_2_5": small,
        },
    )


def audit_rps(con: sqlite3.Connection) -> TableAudit:
    tables = _tables(con)
    if "rps_daily" not in tables:
        return TableAudit("rps_daily", False, notes=["表不存在"])
    rows, first, last, days = _one(
        con,
        "select count(*), min(trade_date), max(trade_date), count(distinct trade_date) from rps_daily",
    )
    per_day = con.execute(
        "select trade_date, count(*), "
        "sum(case when rps50 is not null then 1 else 0 end), "
        "sum(case when rps120 is not null then 1 else 0 end), "
        "sum(case when rps250 is not null then 1 else 0 end) "
        "from rps_daily group by trade_date order by trade_date"
    ).fetchall()
    notes = []
    if days and days < 60:
        notes.append(f"只有 {days} 个扫描日：历史 RPS 不可直接用于 60 日以上验证，需回补")
    return TableAudit(
        "rps_daily",
        True,
        rows=rows,
        first=_date_str(first),
        last=_date_str(last),
        days=days,
        grade=_grade(days / 250, days >= 250),
        notes=notes,
        detail={
            "per_day": [
                {
                    "date": _date_str(d),
                    "rows": int(c),
                    "rps50": int(a),
                    "rps120": int(b),
                    "rps250": int(c250),
                }
                for d, c, a, b, c250 in per_day
            ]
        },
    )


def audit_simple(con: sqlite3.Connection, name: str, date_col: str = "trade_date") -> TableAudit:
    tables = _tables(con)
    if name not in tables:
        return TableAudit(name, False, notes=["表不存在"])
    rows, first, last, days = _one(
        con,
        f"select count(*), min({date_col}), max({date_col}), count(distinct {date_col}) from {name}",
    )
    return TableAudit(
        name,
        True,
        rows=rows,
        first=_date_str(first),
        last=_date_str(last),
        days=days,
        grade=_grade((days or 0) / 250, (days or 0) >= 250),
    )


def audit_sector_basic(con: sqlite3.Connection) -> TableAudit:
    tables = _tables(con)
    if "sector_basic" not in tables:
        return TableAudit("sector_basic", False, notes=["表不存在"])
    rows = _one(con, "select count(*) from sector_basic")[0]
    by_tax = con.execute("select taxonomy, count(*) from sector_basic group by taxonomy").fetchall()
    in_daily = set()
    if "sector_daily" in tables:
        in_daily = {r[0] for r in con.execute("select distinct sector_code from sector_daily")}
    basic = {r[0] for r in con.execute("select code from sector_basic")}
    only_daily = sorted(in_daily - basic)
    only_basic = sorted(basic - in_daily)
    notes = []
    if only_daily:
        notes.append(f"{len(only_daily)} 个板块只在 sector_daily 出现、不在 sector_basic（历史已退出/改名）")
    if only_basic:
        notes.append(f"{len(only_basic)} 个板块只在 sector_basic 出现、从未有日线")
    return TableAudit(
        "sector_basic",
        True,
        rows=rows,
        grade="B" if rows else "D",
        notes=notes,
        detail={
            "taxonomies": [{"taxonomy": t, "count": int(c)} for t, c in by_tax],
            "only_in_daily": len(only_daily),
            "only_in_basic": len(only_basic),
            "only_in_daily_samples": only_daily[:10],
        },
    )


def audit_sector_lifecycle(con: sqlite3.Connection) -> TableAudit:
    """板块存活偏差：有日线但最后日期早于全库最后日期的板块。"""
    tables = _tables(con)
    if "sector_daily" not in tables:
        return TableAudit("sector_lifecycle", False, notes=["sector_daily 不存在"])
    last_all = con.execute("select max(trade_date) from sector_daily").fetchone()[0]
    rows = con.execute(
        "select taxonomy, sector_code, name, min(trade_date), max(trade_date), count(*) "
        "from sector_daily group by taxonomy, sector_code"
    ).fetchall()
    disappeared = [r for r in rows if str(r[4]) < str(last_all)]
    renamed = _one(
        con,
        "select count(*) from (select sector_code, taxonomy from sector_daily "
        "group by sector_code, taxonomy having count(distinct name) > 1)",
    )[0]
    notes = []
    if disappeared:
        notes.append(f"{len(disappeared)} 个板块的最后日期早于全库最后日期（退出/停更/改名）")
    return TableAudit(
        "sector_lifecycle",
        True,
        rows=len(rows),
        days=None,
        grade="B" if rows else "D",
        notes=notes,
        detail={
            "last_date_all": _date_str(last_all),
            "boards": len(rows),
            "disappeared": len(disappeared),
            "disappeared_samples": [
                {"taxonomy": r[0], "code": r[1], "name": r[2], "first": _date_str(r[3]), "last": _date_str(r[4])}
                for r in disappeared[:15]
            ],
            "renamed_boards": renamed,
        },
    )


def audit_day_completeness(con: sqlite3.Connection) -> TableAudit:
    """逐日股票池完整性：与 20 日滚动中位数比较，找出覆盖不足的日期。"""
    tables = _tables(con)
    if "stock_daily" not in tables:
        return TableAudit("stock_pool_daily", False)
    rows = [(_date_str(d), int(c)) for d, c in con.execute(
        "select trade_date, count(*) from stock_daily group by trade_date order by trade_date"
    )]
    if not rows:
        return TableAudit("stock_pool_daily", False, notes=["无数据"])
    counts = [c for _d, c in rows]
    med_all = statistics.median(counts)
    bad = []
    for i, (day, cnt) in enumerate(rows):
        window = counts[max(0, i - 20): i + 1]
        med = statistics.median(window) if window else med_all
        if med and cnt < med * 0.8:
            bad.append({"date": day, "rows": cnt, "rolling_median": med, "ratio": round(cnt / med, 3)})
    notes = []
    if bad:
        notes.append(f"{len(bad)} 个交易日行数低于 20 日滚动中位数的 80%（采集不全的候选）")
    return TableAudit(
        "stock_pool_daily",
        True,
        rows=len(rows),
        first=rows[0][0],
        last=rows[-1][0],
        days=len(rows),
        grade="A" if not bad else "B",
        notes=notes,
        detail={"rolling_median_all": med_all, "bad_days": bad[:30], "bad_day_count": len(bad)},
    )


def audit_st_state(con: sqlite3.Connection) -> TableAudit:
    """历史 ST 状态可验证性：逐日 name 是否可用、ST 数量变化。"""
    tables = _tables(con)
    if "stock_daily" not in tables:
        return TableAudit("st_state", False)
    total, empty = _one(
        con,
        "select count(*), sum(case when name is null or name='' then 1 else 0 end) from stock_daily",
    )
    st_days = [
        (_date_str(d), int(c)) for d, c in con.execute(
            "select trade_date, count(*) from stock_daily where upper(name) like '%ST%' "
            "group by trade_date order by trade_date"
        )
    ]
    notes = []
    if empty:
        notes.append(f"{empty}/{total} 行 name 为空：历史 ST 判定会漏")
    else:
        notes.append("逐日 name 100% 非空：可按当日 name 判历史 ST（比「最新名字」可靠）")
    return TableAudit(
        "st_state",
        True,
        rows=total,
        first=st_days[0][0] if st_days else None,
        last=st_days[-1][0] if st_days else None,
        days=len(st_days),
        grade="A" if not empty else "C",
        notes=notes,
        detail={"st_days": st_days[-10:], "name_empty_rows": empty},
    )


def render_markdown(audits: list[TableAudit], meta: dict) -> str:
    lines = ["# B0.1 历史数据完整性审计（自动生成）", ""]
    lines.append("## 元信息")
    lines.append("")
    for key, value in meta.items():
        lines.append(f"- **{key}**：{value}")
    lines.append("")
    lines.append("## 表级结论")
    lines.append("")
    lines.append("| 表 | 存在 | 行数 | 起 | 止 | 天数 | 等级 | 说明 |")
    lines.append("| --- | --- | ---: | --- | --- | ---: | --- | --- |")
    for item in audits:
        notes = "；".join(item.notes) if item.notes else ""
        lines.append(
            f"| {item.name} | {'是' if item.exists else '否'} | {item.rows if item.rows is not None else '—'} | "
            f"{item.first or '—'} | {item.last or '—'} | {item.days if item.days is not None else '—'} | "
            f"{item.grade} | {notes} |"
        )
    lines.append("")
    lines.append("## 明细")
    lines.append("")
    for item in audits:
        if not item.detail:
            continue
        lines.append(f"### {item.name}")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(item.detail, ensure_ascii=False, indent=2, default=str))
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="SQLite 数据库路径（只读打开）")
    parser.add_argument("--json", help="额外输出 JSON 的路径")
    args = parser.parse_args()

    path = Path(args.db)
    con = _open_ro(path)
    tables = _tables(con)
    cal = []
    if "trade_calendar" in tables:
        cal = [str(r[0]) for r in con.execute("select trade_date from trade_calendar order by trade_date")]
    # 日历是预置到未来的，审计只用「不晚于行情最后日」的部分
    data_last = None
    if "stock_daily" in tables:
        data_last = con.execute("select max(trade_date) from stock_daily").fetchone()[0]
    if cal and data_last is not None:
        cal = [d for d in cal if d <= str(data_last)]

    meta = {
        "数据库": str(path.resolve()),
        "大小(MB)": round(path.stat().st_size / 1024 / 1024, 1),
        "修改时间": date.fromtimestamp(path.stat().st_mtime).isoformat(),
        "包含表": ", ".join(tables),
        "交易日历": "trade_calendar" if "trade_calendar" in tables else "无（退化为日线日期并集）",
        "审计口径": "只读 mode=ro；完整日=行数≥窗口最大行数×50%；ST 用当日 name；退市用最后日期近似",
        "数据最后日": _date_str(data_last),
    }

    audits = [
        audit_stock_daily(con, cal or [str(r[0]) for r in con.execute(
            "select distinct trade_date from stock_daily order by trade_date"
        )] if "stock_daily" in tables else [], "主库" if "trade_calendar" in tables else "长历史库"),
        audit_day_completeness(con),
        audit_st_state(con),
        audit_sector_basic(con),
        audit_sector_daily(con),
        audit_sector_lifecycle(con),
        audit_sector_member(con),
        audit_rps(con),
        audit_simple(con, "limit_pool"),
        audit_simple(con, "pattern_hit"),
        audit_simple(con, "index_daily"),
        audit_simple(con, "market_sentiment"),
        audit_simple(con, "sector_fund_flow"),
        audit_simple(con, "stock_concept"),
    ]

    text = render_markdown(audits, meta)
    print(text)
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "meta": meta,
                    "tables": [
                        {
                            "name": a.name,
                            "exists": a.exists,
                            "rows": a.rows,
                            "first": a.first,
                            "last": a.last,
                            "days": a.days,
                            "grade": a.grade,
                            "notes": a.notes,
                            "detail": a.detail,
                        }
                        for a in audits
                    ],
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
    con.close()


if __name__ == "__main__":
    main()
