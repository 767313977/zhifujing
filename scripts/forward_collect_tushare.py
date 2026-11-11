"""Tushare `daily` → 独立前瞻研究库（`forward_research.db`）的单日采集器。

## 这个脚本解决什么问题

历史库事故（2026-09-28 起 133 只深主板证券整段缺失）暴露的不是"取数源不好"，
而是**采集流程允许"看起来成功"**：失败仍返回 0、没有逐日完成标记、漏采不自愈。
本脚本按相反的规则写：

1. 一天一次 `pro.daily(trade_date=D)`，不做分页、不做补采；
2. **先校验、后提交**：所有校验在写库前完成；写库是一个事务，失败即回滚；
3. 只有事务内写入行数、唯一代码数、完成标记全部对上，才写当日 `data_revision`；
4. 同一天已确认 → 直接拒绝（退出码 2），不发请求、不覆盖；
5. 任何异常 → 退出码非 0，并把错误类型与批次摘要写进 `ingest_log`；
6. **不写** `history.db` / `fupan.db`，只写 `forward_research.db`。

`pro.daily` 的字段与单位（官方口径）：

| 原始 | 研究字段 | 换算 |
| --- | --- | --- |
| `ts_code`（`000001.SZ`） | `code` / `raw_code` | 6 位裸码 / 原样保留 |
| `open/high/low/close` | 同名 | 不复权，元 |
| `pct_chg` | `pct_chg` | 百分数，不除 100 |
| `vol` | `volume` | **× 100 → 股** |
| `amount` | `amount` | **× 1000 → 元** |

## 完整性不变量

- 唯一代码 == 行数（重复即整批拒绝）；
- 7 个必填字段无空值；OHLC > 0；`high >= low`；
- 行数 < 6,000（达到上限视为疑似触顶，整批拒绝）；
- 研究清单覆盖率 ≥ `--min-universe-coverage`（默认 98.5%）；
- 与上一确认日相比，任一板块行数不得低于其 95%；
- 单位自洽：`amount / volume` 与收盘价比值必须落在 [0.2, 5]，否则判为"单位错误"。

当日成功返回但清单内某只证券没有行 → 记为 `suspended_no_quote`（**不伪造 OHLC**），
并写入 `universe_status`；这不是"采集失败"，但也不是"全市场完整"之外的正常停牌。
接口异常 / 空结果 / 超出上限 → 记为 `failed`，**不写任何正式行情**。

## 用法

    # 只看计划（不发请求、不建库）
    .venv\\Scripts\\python.exe scripts\\forward_collect_tushare.py --date 2026-11-12

    # 授权后执行（只发一次请求）
    .venv\\Scripts\\python.exe scripts\\forward_collect_tushare.py --date 2026-11-12 --execute

Token 只从环境变量 / `.env` 读取，任何输出都不打印。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
for _path in (ROOT, BACKEND):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

try:
    import forward_store as store  # noqa: E402  (直接运行脚本时 scripts 在 sys.path)
except ImportError:  # 作为包导入时（pytest 从仓库根收集）
    from scripts import forward_store as store  # noqa: E402

DEFAULT_DB = BACKEND / "data" / "forward_research.db"
DEFAULT_MANIFEST = BACKEND / "data" / "c0_universe_b1_verified.json"
DEFAULT_CALENDAR_DB = BACKEND / "data" / "fupan.db"

#: 与研究库同名的禁用目标：任何情况下都不允许写这些文件
FORBIDDEN_DB_NAMES = {
    "history.db",
    "fupan.db",
    "b0_trial.db",
    "b0_trial_before_rollback.db",
    "b0_trial_before_rps.db",
    "b1_research.db",
    "b2_final_snapshot.db",
}

MAX_ROWS = 6000
MIN_UNIVERSE_COVERAGE = 0.985
MIN_BOARD_COVERAGE = 0.95
MIN_BOARD_ROWS = 20
READY_AT = dt_time(15, 30)
REQUIRED_COLUMNS = ("ts_code", "open", "high", "low", "close", "pct_chg", "vol", "amount")

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_ALREADY = 2
EXIT_API_ERROR = 3
EXIT_DB_ERROR = 4


@dataclass
class Batch:
    """一次取数的规范化结果（纯内存，未落库）。"""

    trade_date: date
    rows: list[dict] = field(default_factory=list)
    statuses: list[tuple[str, str, str]] = field(default_factory=list)
    by_board: dict[str, int] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    data_version: str = ""
    digest: str = ""


class BatchRejected(Exception):
    """整批拒绝（字段/单位/覆盖/截断）。`error_type` 用于 ingest_log。"""

    def __init__(self, message: str, *, error_type: str = "validation", problems: list[str] | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.problems = problems or []


# ---------------------------------------------------------------------------
# 纯函数：规范化与校验
# ---------------------------------------------------------------------------


def _to_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text in ("", "-", "--", "None", "nan", "NaN"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def code_of(ts_code: str) -> str:
    return str(ts_code).split(".")[0]


def batch_digest(rows: list[dict]) -> str:
    """批次内容哈希：只含行情字段，不含 `ingested_at`（同数据重跑得到同一版本）。"""
    payload = "\n".join(
        "|".join(
            [
                row["code"],
                f"{row['open']:.6f}",
                f"{row['high']:.6f}",
                f"{row['low']:.6f}",
                f"{row['close']:.6f}",
                f"{row['pct_chg']:.6f}",
                f"{row['volume']:.6f}",
                f"{row['amount']:.6f}",
            ]
        )
        for row in sorted(rows, key=lambda item: item["code"])
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_frame(
    raw_rows: list[dict],
    day: date,
    *,
    manifest_codes: set[str],
    ingested_at: str,
    data_version: str,
) -> Batch:
    """把 Tushare 原始行转成研究行；任何字段/单位问题 → 整批 `BatchRejected`。"""
    if not raw_rows:
        raise BatchRejected("接口返回空结果（不能解释为全市场停牌）", error_type="empty")
    if not raw_rows or not set(REQUIRED_COLUMNS).issubset(raw_rows[0]):
        missing = sorted(set(REQUIRED_COLUMNS) - set(raw_rows[0] if raw_rows else {}))
        raise BatchRejected(f"返回缺少字段：{missing}", error_type="schema")

    rows: list[dict] = []
    problems: list[str] = []
    seen: set[str] = set()
    duplicates: list[str] = []

    for raw in raw_rows:
        raw_code = str(raw.get("ts_code") or "")
        try:
            code, _ = store.normalize_code(raw_code)
        except ValueError as error:
            problems.append(str(error))
            continue
        if code in seen:
            duplicates.append(code)
            continue
        seen.add(code)

        values = {name: _to_float(raw.get(name)) for name in REQUIRED_COLUMNS if name != "ts_code"}
        missing_fields = [name for name, value in values.items() if value is None]
        if missing_fields:
            problems.append(f"{code} 缺字段 {missing_fields}（成交额缺失时不允许估算）")
            continue
        close = values["close"]
        if close <= 0 or min(values["open"], values["high"], values["low"]) <= 0:
            problems.append(f"{code} 价格必须为正")
            continue
        if values["high"] < values["low"]:
            problems.append(f"{code} high < low：{values['high']} < {values['low']}")
            continue
        if not (-100 < values["pct_chg"] < 1000):
            problems.append(f"{code} pct_chg 超出合理范围：{values['pct_chg']}")
            continue

        volume = values["vol"] * 100.0
        amount = values["amount"] * 1000.0
        if volume <= 0 or amount <= 0:
            problems.append(f"{code} 成交量/成交额必须为正（vol={values['vol']}, amount={values['amount']}）")
            continue
        implied = amount / volume
        if not (0.2 * close <= implied <= 5.0 * close):
            problems.append(
                f"{code} 单位不自洽：成交额/成交量={implied:.2f}，收盘={close:.2f}"
                "（疑似 vol 已是股或 amount 已是元）"
            )
            continue

        row = {
            "trade_date": day.isoformat(),
            "code": code,
            "name": None,
            "open": values["open"],
            "high": values["high"],
            "low": values["low"],
            "close": close,
            "pct_chg": values["pct_chg"],
            "volume": volume,
            "amount": amount,
            "turnover": None,
            "board": store.board_of(code),
            "source": "tushare",
            "pct_source": "official",
            "raw_code": raw_code,
            "data_version": data_version,
            "ingested_at": ingested_at,
        }
        row_problems = store.validate_row(row)
        if row_problems:
            problems.extend(f"{code} {item}" for item in row_problems)
            continue
        rows.append(row)

    if duplicates:
        problems.append(f"重复代码 {len(duplicates)} 个：{sorted(set(duplicates))[:10]}")
    if problems:
        raise BatchRejected(
            f"{len(problems)} 处数据问题，整批拒绝（未写库）",
            error_type="row_invalid",
            problems=problems[:50],
        )

    codes = {row["code"] for row in rows}
    by_board: dict[str, int] = {}
    for row in rows:
        by_board[row["board"]] = by_board.get(row["board"], 0) + 1
    missing = sorted(manifest_codes - codes)
    missing_set = set(missing)
    statuses = [
        (
            code,
            "suspended_no_quote" if code in missing_set else "ok",
            "absent_from_daily" if code in missing_set else "",
        )
        for code in sorted(manifest_codes)
    ]
    digest = batch_digest(rows)
    return Batch(
        trade_date=day,
        rows=rows,
        statuses=statuses,
        by_board=by_board,
        missing=missing,
        data_version=data_version,
        digest=digest,
    )


def batch_data_version(day: date, digest: str) -> str:
    return f"tushare-daily:{day.isoformat()}:{digest[:12]}"


def validate_batch(
    batch: Batch,
    *,
    manifest_codes: set[str],
    previous_board: dict[str, int] | None = None,
    min_universe_coverage: float = MIN_UNIVERSE_COVERAGE,
) -> list[str]:
    """整批完整性校验；返回问题列表（空 = 可提交）。"""
    problems: list[str] = []
    rows = len(batch.rows)
    if rows == 0:
        return ["当日没有任何有效行情行"]
    if rows >= MAX_ROWS:
        problems.append(f"返回 {rows} 行，达到 {MAX_ROWS} 上限：疑似触顶，必须分段/拒绝")
    unique = {row["code"] for row in batch.rows}
    if len(unique) != rows:
        problems.append(f"唯一代码 {len(unique)} != 行数 {rows}：存在重复行")

    matched = len(unique & manifest_codes)
    coverage = matched / len(manifest_codes) if manifest_codes else 0.0
    if coverage < min_universe_coverage:
        problems.append(
            f"研究清单覆盖 {matched}/{len(manifest_codes)} = {coverage:.2%}"
            f"（< {min_universe_coverage:.1%}）：疑似整段证券缺失"
        )

    for board, count in batch.by_board.items():
        if count <= 0:
            problems.append(f"{board} 当日 0 行：该市场整段缺失")
    if previous_board:
        for board, count in previous_board.items():
            if count < MIN_BOARD_ROWS:
                continue
            today_rows = batch.by_board.get(board, 0)
            ratio = today_rows / count
            if ratio < MIN_BOARD_COVERAGE:
                problems.append(
                    f"{board} 覆盖 {today_rows} 行，上一确认日 {count} 行，仅 {ratio:.1%}"
                    f"（< {MIN_BOARD_COVERAGE:.0%}）：疑似该市场被静默丢弃"
                )
    return problems


# ---------------------------------------------------------------------------
# 数据库：读取前置条件 + 单事务写入
# ---------------------------------------------------------------------------


def _check_target(path: Path) -> None:
    if path.name in FORBIDDEN_DB_NAMES:
        raise SystemExit(f"拒绝写入非前瞻库：{path}")
    if path.name != "forward_research.db":
        raise SystemExit(
            f"目标库名必须是 forward_research.db（当前 {path.name}）；独立研究链路不与业务库混用"
        )


def load_manifest_codes(path: Path) -> set[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {item["code"] for item in payload["securities"]}


def load_calendar(db_path: Path, day: date, span: int = 15) -> list[date]:
    """从本地可信日历读取 `day` 附近的交易日（只读）。"""
    if not db_path.is_file():
        raise SystemExit(f"没有交易日历库 {db_path}")
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        low = (day - timedelta(days=span)).isoformat()
        high = (day + timedelta(days=span)).isoformat()
        return [
            date.fromisoformat(row[0])
            for row in con.execute(
                "SELECT trade_date FROM trade_calendar WHERE trade_date BETWEEN ? AND ? "
                "ORDER BY trade_date",
                (low, high),
            )
        ]
    finally:
        con.close()


def connect_target(path: Path, *, create: bool) -> store.Engine:  # type: ignore[attr-defined]
    if create:
        engine = store.connect(path)
        store.create_schema(engine)
        return engine
    return store.connect_readonly(path)


def _day_revision(conn, day: date) -> int | None:
    row = conn.exec_driver_sql(
        "SELECT revision FROM data_revision WHERE domain=?", (f"stock_daily:{day.isoformat()}",)
    ).fetchone()
    return int(row[0]) if row else None


def _previous_board_counts(conn, day: date) -> dict[str, int] | None:
    """上一确认日（有 `stock_daily:D` 完成标记）的分板块行数。"""
    row = conn.exec_driver_sql(
        "SELECT domain FROM data_revision WHERE domain LIKE 'stock_daily:%' "
        "AND domain < ? ORDER BY domain DESC LIMIT 1",
        (f"stock_daily:{day.isoformat()}",),
    ).fetchone()
    if not row:
        return None
    previous = row[0].split(":", 1)[1]
    counts: dict[str, int] = {}
    for code, count in conn.exec_driver_sql(
        "SELECT substr(code,1,6), COUNT(*) FROM stock_daily WHERE trade_date=? GROUP BY substr(code,1,6)",
        (previous,),
    ):
        board = store.board_of(str(code))
        counts[board] = counts.get(board, 0) + int(count)
    return counts


def _record_ingest(
    engine,
    day: date,
    *,
    status: str,
    rows: int | None,
    message: dict,
    now: datetime,
) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO ingest_log(trade_date, status, rows, message, created_at) VALUES (?,?,?,?,?)",
            (day.isoformat(), status, rows, json.dumps(message, ensure_ascii=False), now.isoformat()),
        )


def _write_confirmed(engine, batch: Batch, *, now: datetime, revise: bool) -> None:
    """单事务：写行情 + 状态 + 完成标记；任一步失败整批回滚。"""
    day_text = batch.trade_date.isoformat()
    with engine.begin() as conn:
        existing = _day_revision(conn, batch.trade_date)
        if existing and not revise:
            raise BatchRejected(f"{day_text} 已有完成标记 revision={existing}，拒绝覆盖", error_type="already_confirmed")
        if revise:
            conn.exec_driver_sql("DELETE FROM stock_daily WHERE trade_date=?", (day_text,))
            conn.exec_driver_sql("DELETE FROM universe_status WHERE trade_date=?", (day_text,))

        for row in batch.rows:
            conn.exec_driver_sql(
                """
                INSERT INTO stock_daily
                (trade_date, code, name, open, high, low, close, pct_chg,
                 volume, amount, turnover, board, source, pct_source,
                 raw_code, data_version, ingested_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["trade_date"], row["code"], row["name"], row["open"],
                    row["high"], row["low"], row["close"], row["pct_chg"],
                    row["volume"], row["amount"], row["turnover"], row["board"],
                    row["source"], row["pct_source"], row["raw_code"],
                    row["data_version"], row["ingested_at"],
                ),
            )
        for code, status, reason in batch.statuses:
            conn.exec_driver_sql(
                "INSERT INTO universe_status(trade_date, code, status, reason) VALUES (?,?,?,?)",
                (day_text, code, status, reason),
            )

        written = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM stock_daily WHERE trade_date=?", (day_text,)
        ).scalar()
        if int(written or 0) != len(batch.rows):
            raise RuntimeError(f"事务内行数校验失败：写 {written}，应为 {len(batch.rows)}")

        revision = 1 if not existing else existing + 1
        for domain in (f"stock_daily:{day_text}", "stock_daily"):
            conn.exec_driver_sql(
                """
                INSERT INTO data_revision(domain, revision, updated_at) VALUES (?,?,?)
                ON CONFLICT(domain) DO UPDATE SET revision=revision+1, updated_at=excluded.updated_at
                """,
                (domain, revision, now.isoformat()),
            )


def already_confirmed(db_path: Path, day: date) -> bool:
    if not db_path.is_file():
        return False
    engine = store.connect_readonly(db_path)
    try:
        with engine.connect() as conn:
            return _day_revision(conn, day) is not None
    except Exception:  # noqa: BLE001 - 空文件/未建表都按「未确认」处理
        return False
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------


def _safe_record_failure(
    db_path: Path,
    day: date,
    error_type: str,
    message: str,
    problems: list[str],
    elapsed_ms: int,
    now: datetime,
) -> None:
    """失败留痕；留痕本身失败也不能掩盖原始错误。"""
    try:
        engine = connect_target(db_path, create=True)
        try:
            _record_ingest(
                engine,
                day,
                status="failed",
                rows=None,
                message={
                    "error_type": error_type,
                    "error": _safe(message)[:1000],
                    "problems": problems[:20],
                    "elapsed_ms": elapsed_ms,
                    "request_date": day.isoformat(),
                },
                now=now,
            )
        finally:
            engine.dispose()
    except Exception:  # noqa: BLE001
        pass


def collect(
    db_path: Path,
    day: date,
    *,
    fetch,
    manifest_codes: set[str],
    calendar: list[date],
    now: datetime,
    execute: bool,
    revise: bool = False,
    min_universe_coverage: float = MIN_UNIVERSE_COVERAGE,
) -> dict:
    """执行/预演一天。`fetch(day) -> list[dict]` 由调用方注入（测试用合成数据）。"""
    if day not in calendar:
        raise BatchRejected(f"{day} 不在交易日历里，不是有效交易日", error_type="calendar")
    if now.tzinfo is None:
        raise ValueError("now 必须带时区")
    ready_at = datetime.combine(day, READY_AT, tzinfo=now.tzinfo)
    if now < ready_at:
        raise BatchRejected(
            f"当前 {now:%Y-%m-%d %H:%M} 早于 {day} {READY_AT:%H:%M}：收盘数据未就绪",
            error_type="not_ready",
        )
    if already_confirmed(db_path, day) and not revise:
        return {"status": "already_confirmed", "exit_code": EXIT_ALREADY, "day": day.isoformat()}

    if not execute:
        return {
            "status": "plan",
            "exit_code": EXIT_OK,
            "day": day.isoformat(),
            "calendar_ok": True,
            "manifest": len(manifest_codes),
            "db_exists": db_path.is_file(),
        }

    started = time.monotonic()
    ingested_at = now.isoformat()
    try:
        raw = fetch(day)
    except Exception as error:  # noqa: BLE001
        elapsed_ms = int((time.monotonic() - started) * 1000)
        _safe_record_failure(db_path, day, "api", str(error), [], elapsed_ms, now)
        raise BatchRejected(f"接口调用失败（未重试）：{_safe(str(error))}", error_type="api") from error

    elapsed_ms = int((time.monotonic() - started) * 1000)
    try:
        batch = normalize_frame(raw, day, manifest_codes=manifest_codes,
                                ingested_at=ingested_at, data_version="pending")
        batch.data_version = batch_data_version(day, batch.digest)
        for row in batch.rows:
            row["data_version"] = batch.data_version
        engine = connect_target(db_path, create=True)
        try:
            with engine.connect() as conn:
                previous_board = _previous_board_counts(conn, day)
            problems = validate_batch(
                batch, manifest_codes=manifest_codes,
                previous_board=previous_board,
                min_universe_coverage=min_universe_coverage,
            )
            if problems:
                raise BatchRejected("完整性校验未通过", error_type="incomplete", problems=problems)
            _write_confirmed(engine, batch, now=now, revise=revise)
            _record_ingest(
                engine, day, status="ok", rows=len(batch.rows),
                message={
                    "rows": len(batch.rows),
                    "unique": len({row['code'] for row in batch.rows}),
                    "by_board": batch.by_board,
                    "missing": len(batch.missing),
                    "missing_sample": batch.missing[:20],
                    "elapsed_ms": elapsed_ms,
                    "request_date": day.isoformat(),
                    "data_version": batch.data_version,
                    "revised": bool(revise),
                },
                now=now,
            )
        finally:
            engine.dispose()
    except BatchRejected as error:
        _safe_record_failure(db_path, day, error.error_type, str(error), error.problems, elapsed_ms, now)
        raise
    except Exception as error:  # noqa: BLE001 - 写库/未知错误：先留痕再向上抛
        _safe_record_failure(db_path, day, "db", str(error), [], elapsed_ms, now)
        raise

    return {
        "status": "confirmed",
        "exit_code": EXIT_OK,
        "day": day.isoformat(),
        "rows": len(batch.rows),
        "by_board": batch.by_board,
        "missing": len(batch.missing),
        "data_version": batch.data_version,
        "elapsed_ms": elapsed_ms,
    }


def _safe(text: str) -> str:
    token = os.environ.get("TUSHARE_TOKEN", "")
    return text.replace(token, "***") if token else text


def _fetch_tushare(day: date):
    from scripts.verify_tushare_daily import load_token

    token = load_token()
    if not token:
        raise RuntimeError("未配置 TUSHARE_TOKEN（环境变量或 .env）")
    import tushare as ts

    pro = ts.pro_api(token)
    frame = pro.daily(trade_date=day.strftime("%Y%m%d"))
    if hasattr(frame, "to_dict"):
        return frame.to_dict("records")
    return list(frame)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tushare daily 单日写入前瞻研究库（默认只预演）")
    parser.add_argument("--date", required=True, help="交易日 YYYY-MM-DD")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--calendar-db", default=str(DEFAULT_CALENDAR_DB))
    parser.add_argument("--execute", action="store_true", help="真正发送一次请求并写库")
    parser.add_argument("--revise", action="store_true", help="已有完成标记时允许显式修订（留痕）")
    parser.add_argument("--min-universe-coverage", type=float, default=MIN_UNIVERSE_COVERAGE)
    args = parser.parse_args(argv)

    day = date.fromisoformat(args.date)
    db_path = Path(args.db)
    _check_target(db_path)
    manifest = load_manifest_codes(Path(args.manifest))
    calendar = load_calendar(Path(args.calendar_db), day)
    now = datetime.now().astimezone()

    if not args.execute:
        print(f"计划：daily(trade_date={day:%Y%m%d})；清单 {len(manifest)} 只；"
              f"日历{'包含' if day in calendar else '不包含'}该日；目标 {db_path.name}")
        print("[dry-run] 未发送请求、未建库。")
        return EXIT_OK

    try:
        result = collect(
            db_path, day,
            fetch=_fetch_tushare,
            manifest_codes=manifest,
            calendar=calendar,
            now=now,
            execute=True,
            revise=args.revise,
            min_universe_coverage=args.min_universe_coverage,
        )
    except BatchRejected as error:
        print(f"拒绝：{error}（error_type={error.error_type}）")
        for item in error.problems[:20]:
            print(f"  - {item}")
        return EXIT_API_ERROR if error.error_type == "api" else EXIT_REJECTED
    except Exception as error:  # noqa: BLE001
        print(f"数据库/未知错误：{_safe(str(error))}")
        return EXIT_DB_ERROR

    if result["status"] == "already_confirmed":
        print(f"{result['day']} 已有完成标记：拒绝重复写入，未发请求。")
        return EXIT_ALREADY
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
