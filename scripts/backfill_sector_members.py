"""回补 `sector_member`（板块成分股）的历史快照。

## 为什么需要它

`sector_member` 只有两条写入路径：当晚 22:00 的 `collect_all_members`，和打开板块时
现取（`api/sector._board_members`）。所以 2026-09-28 之前的日子基本是空的（只有零星
几个板块被「打开过」）。想从板块强度落到个股、并且回测，就必须有**逐日、全板块**的
成分股 —— 拿今天的成分回看历史是前视偏差（调入调出的票会被当成「一直在里面」）。

开盘红的 `ZhiShuStockList_W8` 支持历史日期，且返回的是**当时**的成分：实测 2026-03-02
的 801001 是 970 只、2026-10-23 是 1136 只 —— 说明是真历史，不是当前快照。

## B0 安全约束（2026-11-08）

1. **必须显式指定 `--db`**，且默认拒绝写 `backend/data/fupan.db`（生产/主库）。
   历史回补只允许写隔离副本；要写主库必须额外加 `--allow-main-db`（不推荐）。
2. **目标板块取当天 `sector_daily` 的板块清单**，不是当前 `sector_basic` ——
   避免把「今天还在、历史上还没出现」的板块算进缺口，也避免漏掉已退出/改名的板块。
3. **完整性按来源 `Count` 对账**（开盘红成分股接口的 Count 是真总数）：
   - `complete`：`reported` 已知且 `unique >= reported`
   - `partial`：`reported` 已知且 `0 < unique < reported`
   - `source_empty`：`reported == 0` 且没有取到任何行
   - `unverified`：来源没给 `Count`，无法对账（不能标完整）
   - `failed`：请求异常（含重试耗尽）
   - `unsupported_history`：当天没有 `sector_daily` 板块清单，或来源明确不支持该日期
   绝不因为「表里有一行」就判定完整。
4. 断点续跑：manifest 表 `sector_member_backfill` 记录每个 (日期, 板块) 的状态；
   默认只重试 `not_collected / failed / unverified / partial`，`complete / source_empty`
   直接跳过（`--force` 可重取）。
5. 复制数据库必须用 SQLite 在线备份 API（`--copy-from`），不能直接复制 WAL 主文件。

## 用法

    # 1) 用在线备份 API 建隔离副本
    python scripts/backfill_sector_members.py --db data/b0_trial.db \
        --copy-from backend/data/fupan.db

    # 2) 只审计 manifest（不联网）
    python scripts/backfill_sector_members.py --db data/b0_trial.db --audit-only

    # 3) 小范围试验：3 个交易日、最多 6 个板块
    python scripts/backfill_sector_members.py --db data/b0_trial.db \
        --start 2026-09-24 --end 2026-09-24 --limit-boards 6

    # 4) 获批后分批回补最近 60 个交易日（显式上限，防止误扩）
    python scripts/backfill_sector_members.py --db data/b0_trial.db \
        --days 60 --max-days 60

退出码：0 = 无 failed；2 = 有 failed / unsupported_history（需要人看报告）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
BACKEND = ROOT / "backend"
for path in (str(BACKEND), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

logger = logging.getLogger("backfill")

MANIFEST_DDL = """
CREATE TABLE IF NOT EXISTS sector_member_backfill (
    trade_date TEXT NOT NULL,
    sector_code TEXT NOT NULL,
    board_name TEXT,
    taxonomy TEXT,
    status TEXT NOT NULL,
    reported INTEGER,
    unique_count INTEGER,
    written INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, sector_code)
)
"""

RUN_DDL = """
CREATE TABLE IF NOT EXISTS sector_member_backfill_run (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    db_path TEXT NOT NULL,
    args_json TEXT,
    summary_json TEXT
)
"""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回补 sector_member 历史成分股（B0 隔离版）")
    parser.add_argument("--db", required=True, help="目标 SQLite（必须显式指定，默认拒绝主库）")
    parser.add_argument("--copy-from", help="先用 SQLite 在线备份 API 从该库复制一份到 --db")
    parser.add_argument("--allow-main-db", action="store_true", help="允许写 backend/data/fupan.db（不推荐）")
    parser.add_argument("--days", type=int, default=3, help="最近 N 个交易日（默认 3，需显式放大）")
    parser.add_argument("--max-days", type=int, default=5, help="本次最多处理多少个交易日（硬上限）")
    parser.add_argument("--start", type=date.fromisoformat, help="起始日（含），优先于 --days")
    parser.add_argument("--end", type=date.fromisoformat, help="截止日（含），缺省=最新日线日")
    parser.add_argument("--boards", help="只处理这些板块代码/名称（逗号分隔）")
    parser.add_argument("--limit-boards", type=int, default=0, help="每个日期最多处理多少个板块（0=全部）")
    parser.add_argument("--retries", type=int, default=2, help="单个板块失败重试次数（默认 2）")
    parser.add_argument("--retry-sleep", type=float, default=2.0, help="重试间隔秒（默认 2）")
    parser.add_argument("--sleep", type=float, default=0.0, help="板块之间额外停顿秒（默认 0，客户端自带限速）")
    parser.add_argument("--force", action="store_true", help="连 complete/source_empty 也重取")
    parser.add_argument("--dry-run", action="store_true", help="只打印目标与缺口，不联网、不写库")
    parser.add_argument("--audit-only", action="store_true", help="只输出 manifest 汇总，不联网")
    parser.add_argument("--report", help="把本次结果写成 JSON")
    return parser.parse_args()


def _is_main_db(path: Path) -> bool:
    return os.path.normcase(str(path.resolve())) == os.path.normcase(
        str((ROOT / "backend" / "data" / "fupan.db").resolve())
    )


def _copy_db(src: Path, dst: Path) -> None:
    """SQLite 在线备份：读取一致性快照，不直接复制 WAL 主文件。"""
    if not src.exists():
        raise SystemExit(f"源库不存在：{src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise SystemExit(f"目标已存在，拒绝覆盖：{dst}")
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(str(dst))
    try:
        source.backup(target)
        target.execute("PRAGMA journal_mode=WAL")
    finally:
        target.close()
        source.close()
    logger.info("已用在线备份 API 复制：%s → %s", src, dst)


def _ensure_manifest(con: sqlite3.Connection) -> None:
    con.execute(MANIFEST_DDL)
    con.execute(RUN_DDL)
    con.commit()


def _manifest_rows(con: sqlite3.Connection, trade_date: date | None = None) -> dict[tuple[str, str], dict]:
    sql = "select trade_date, sector_code, board_name, taxonomy, status, reported, unique_count, written, attempts, last_error from sector_member_backfill"
    params: tuple = ()
    if trade_date is not None:
        sql += " where trade_date = ?"
        params = (trade_date.isoformat(),)
    out = {}
    for row in con.execute(sql, params):
        out[(row[0], row[1])] = {
            "trade_date": row[0],
            "sector_code": row[1],
            "board_name": row[2],
            "taxonomy": row[3],
            "status": row[4],
            "reported": row[5],
            "unique": row[6],
            "written": row[7],
            "attempts": row[8],
            "last_error": row[9],
        }
    return out


def _upsert_manifest(con: sqlite3.Connection, item: dict) -> None:
    con.execute(
        """
        INSERT INTO sector_member_backfill
            (trade_date, sector_code, board_name, taxonomy, status, reported,
             unique_count, written, attempts, last_error, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trade_date, sector_code) DO UPDATE SET
            board_name=excluded.board_name,
            taxonomy=excluded.taxonomy,
            status=excluded.status,
            reported=excluded.reported,
            unique_count=excluded.unique_count,
            written=excluded.written,
            attempts=excluded.attempts,
            last_error=excluded.last_error,
            updated_at=excluded.updated_at
        """,
        (
            item["trade_date"],
            item["sector_code"],
            item.get("board_name"),
            item.get("taxonomy"),
            item["status"],
            item.get("reported"),
            item.get("unique"),
            item.get("written"),
            item.get("attempts", 0),
            item.get("last_error"),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    con.commit()


def _target_days(session, args: argparse.Namespace) -> list[date]:
    """要处理哪几个交易日（升序）。优先 --start/--end；否则最近 --days 个。"""
    from sqlalchemy import func, select

    from app.models import StockDaily, TradeCalendar

    end = args.end or session.scalar(select(func.max(StockDaily.trade_date)))
    if end is None:
        return []
    statement = select(TradeCalendar.trade_date).where(TradeCalendar.trade_date <= end)
    if args.start:
        statement = statement.where(TradeCalendar.trade_date >= args.start)
        days = sorted(session.scalars(statement))
    else:
        days = sorted(
            session.scalars(statement.order_by(TradeCalendar.trade_date.desc()).limit(args.days))
        )
    if len(days) > args.max_days:
        raise SystemExit(
            f"目标区间有 {len(days)} 个交易日，超过 --max-days={args.max_days}。"
            "请显式放大 --max-days（本轮只批准有限范围，不做静默截断）。"
        )
    return days


def _day_boards(session, day: date) -> list[dict]:
    """当天 `sector_daily` 里的板块清单（代码 / 名称 / taxonomy）。"""
    from sqlalchemy import select

    from app.models import SectorDaily

    rows = session.execute(
        select(SectorDaily.sector_code, SectorDaily.name, SectorDaily.taxonomy)
        .where(SectorDaily.trade_date == day)
        .order_by(SectorDaily.sector_code)
    ).all()
    seen: dict[str, dict] = {}
    for code, name, taxonomy in rows:
        if code and code not in seen:
            seen[code] = {"code": code, "name": name, "taxonomy": taxonomy}
    return list(seen.values())


def _classify(reported: int | None, unique: int, written: int, error: str | None) -> tuple[str, str | None]:
    """把一次抓取结果归到可验证状态。返回 (status, note)。"""
    if error:
        if "1020" in error:
            return "unsupported_history", error
        return "failed", error
    if reported is None:
        if unique == 0:
            # 来源没给 Count，又没有行：可能是「还没发布」，不能当完整，也不能当明确无数据
            return "unverified", "来源未给 Count，且未取到任何行"
        return "unverified", "来源未给 Count，无法对账"
    if reported == 0:
        return "source_empty", "来源 Count=0"
    if unique >= reported:
        if written < unique:
            return "partial", f"来源 {reported} 只，落库 {written}/{unique}（写入不完整）"
        return "complete", None
    if unique == 0:
        return "failed", f"来源 Count={reported}，但未取到任何行"
    return "partial", f"来源 {reported} 只，实际 {unique} 只"


def main() -> int:
    args = _parse_args()
    target = Path(args.db).resolve()
    if _is_main_db(target) and not args.allow_main_db:
        raise SystemExit(
            "拒绝写主库 backend/data/fupan.db。历史回补只允许写隔离副本；"
            "确需写主库请显式加 --allow-main-db（不推荐）。"
        )
    if args.copy_from:
        _copy_db(Path(args.copy_from).resolve(), target)
    if not target.exists():
        raise SystemExit(f"目标库不存在：{target}（可先用 --copy-from 复制）")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.environ["DB_PATH"] = str(target)

    # 必须在设置 DB_PATH 之后再导入 app.db（它在模块加载时创建 engine）
    from app.db import session_scope
    from app.jobs.collect_sectors import SectorCollector

    con = sqlite3.connect(str(target), timeout=15)
    if args.audit_only:
        exists = con.execute(
            "select name from sqlite_master where type='table' and name='sector_member_backfill'"
        ).fetchone()
        rows = _manifest_rows(con) if exists else {}
        summary: dict[str, int] = {}
        for item in rows.values():
            summary[item["status"]] = summary.get(item["status"], 0) + 1
        print(json.dumps({"db": str(target), "rows": len(rows), "status": summary}, ensure_ascii=False, indent=2))
        con.close()
        return 0

    with session_scope() as session:
        days = _target_days(session, args)
        if not days:
            logger.warning("没有可用的交易日，先确认 trade_calendar / stock_daily 有数据")
            con.close()
            return 1
        logger.info("目标 %s ~ %s 共 %d 个交易日（上限 %d）", days[0], days[-1], len(days), args.max_days)
        board_filter = {x.strip() for x in (args.boards or "").split(",") if x.strip()}
        plan: list[tuple[date, list[dict]]] = []
        for day in days:
            boards = _day_boards(session, day)
            if board_filter:
                boards = [b for b in boards if b["code"] in board_filter or (b["name"] or "") in board_filter]
            if args.limit_boards:
                boards = boards[: args.limit_boards]
            plan.append((day, boards))
            logger.info("%s：sector_daily 板块 %d 个", day, len(boards))

    if args.dry_run:
        print(
            json.dumps(
                {
                    "db": str(target),
                    "dry_run": True,
                    "days": [d.isoformat() for d, _ in plan],
                    "boards": sum(len(b) for _, b in plan),
                    "detail": [
                        {"date": d.isoformat(), "boards": [b["code"] for b in boards]}
                        for d, boards in plan
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        con.close()
        return 0

    _ensure_manifest(con)
    collector = SectorCollector()
    started = time.monotonic()
    run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    con.execute(
        "insert into sector_member_backfill_run(run_id, started_at, db_path, args_json) values (?, ?, ?, ?)",
        (run_id, datetime.now().isoformat(timespec="seconds"), str(target), json.dumps(vars(args), default=str)),
    )
    con.commit()

    results: list[dict] = []
    for day, boards in plan:
        if not boards:
            results.append(
                {
                    "trade_date": day.isoformat(),
                    "sector_code": "",
                    "status": "unsupported_history",
                    "note": "当天没有 sector_daily 板块清单，无法确定历史板块目录",
                }
            )
            continue
        existing = _manifest_rows(con, day)
        for board in boards:
            key = (day.isoformat(), board["code"])
            old = existing.get(key)
            if old and old["status"] in ("complete", "source_empty") and not args.force:
                continue
            if args.dry_run:
                results.append({**board, "trade_date": day.isoformat(), "status": "not_collected", "note": "dry-run"})
                continue
            stats: dict = {}
            error: str | None = None
            written = 0
            attempts = (old or {}).get("attempts", 0)
            for attempt in range(args.retries + 1):
                attempts += 1
                try:
                    written = collector.collect_members(day, board["code"], stats=stats)
                    error = None
                    break
                except Exception as exc:  # noqa: BLE001 - 单板块失败不拖垮整轮
                    error = f"{type(exc).__name__}: {exc}"
                    if "errcode=1020" in str(exc):
                        error = f"未发布/不支持历史（errcode=1020）：{exc}"
                        break
                    if attempt < args.retries:
                        time.sleep(args.retry_sleep)
            reported = stats.get("reported")
            unique = stats.get("unique", 0 if error else written)
            status, note = _classify(reported, int(unique or 0), int(written or 0), error)
            item = {
                "trade_date": day.isoformat(),
                "sector_code": board["code"],
                "board_name": board["name"],
                "taxonomy": board["taxonomy"],
                "status": status,
                "reported": reported,
                "unique": int(unique or 0),
                "written": int(written or 0),
                "attempts": attempts,
                "last_error": note,
            }
            _upsert_manifest(con, item)
            results.append({**item, "note": note})
            if args.sleep:
                time.sleep(args.sleep)
        logger.info("%s 完成（累计 %d 个板块）", day, len(results))

    summary: dict[str, int] = {}
    for item in results:
        summary[item["status"]] = summary.get(item["status"], 0) + 1
    elapsed = round(time.monotonic() - started, 1)
    payload = {
        "db": str(target),
        "run_id": run_id,
        "days": [d.isoformat() for d, _ in plan],
        "boards": sum(len(b) for _, b in plan),
        "status": summary,
        "elapsed_seconds": elapsed,
        "results": results,
    }
    con.execute(
        "update sector_member_backfill_run set finished_at=?, summary_json=? where run_id=?",
        (datetime.now().isoformat(timespec="seconds"), json.dumps(payload, ensure_ascii=False), run_id),
    )
    con.commit()
    con.close()

    if args.report:
        Path(args.report).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in payload.items() if k != "results"}, ensure_ascii=False, indent=2))
    return 2 if summary.get("failed") or summary.get("unsupported_history") else 0


if __name__ == "__main__":
    raise SystemExit(main())
