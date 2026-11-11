"""Tushare `daily` → 独立前瞻研究库（`forward_research.db`）的单日采集器。

## 这个脚本解决什么问题

历史库事故（2026-09-28 起 133 只深主板证券整段缺失）暴露的不是"取数源不好"，
而是**采集流程允许"看起来成功"**：失败仍返回 0、没有逐日完成标记、漏采不自愈。
本脚本按相反的规则写：

1. 一天一次 `pro.daily(trade_date=D)`，不做分页、不做补采；
2. **先校验、后提交**：所有校验在写库前完成；写库是一个事务，失败即回滚；
3. `stock_daily` / `universe_status` / 当日 `data_revision` / 成功 `ingest_log`
   **在同一次提交内**；任何一步失败都不会留下"完成标记存在、成功日志缺失"；
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

## 完整性基线：首次采集也不能只看总数

分市场基线优先取「上一确认日」，**没有上一确认日时改用冻结清单
`c0_universe_b1_verified.json` 的可前瞻分市场数量**。清单的 `version`、
`eligibility_sha256`、`universe_sha256` 都会重算校验；只读 `forward_eligible`
且 `identity_verified` 的证券，不扩大策略范围。

## 不变量

- 唯一代码 == 行数（重复即整批拒绝）；
- 7 个必填字段无空值；OHLC > 0；`high >= low`；单位自洽；
- 行数 < 6,000（达到上限视为疑似触顶，整批拒绝）；
- 研究清单覆盖率 ≥ `--min-universe-coverage`（默认 98.5%，**下限 95%，不允许调到 0**）；
- 分市场覆盖 ≥ 基线（上一确认日或冻结清单）的 95%；
- 停牌：当日成功返回但清单内某只无行 → `no_quote_unverified`（**不伪造 OHLC**）；
  只有提供 `--suspended-file` 的可信证据才标 `suspended_confirmed`；
  整批失败不写逐券状态（`batch_failed` 只出现在 `ingest_log`）。

## 日历

行情采集**不会**自动写日历。首次使用前单独初始化（只写目标库）：

    python scripts/forward_collect_tushare.py --init-calendar --db backend/data/forward_research.db

采集时目标库 `trade_calendar` 必须已包含当日；否则拒绝，避免 40 日行情写完后
前瞻工具因缺日历而拒绝冻结。

## 用法

    # 只看计划（不发请求、不建库）
    .venv\\Scripts\\python.exe scripts/forward_collect_tushare.py --date 2026-11-12

    # 授权后执行（只发一次请求）
    .venv\\Scripts\\python.exe scripts/forward_collect_tushare.py --date 2026-11-12 --execute

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
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
for _path in (ROOT, BACKEND):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

try:
    import forward_store as store  # noqa: E402  (直接运行脚本时 scripts 在 sys.path)
except ImportError:  # 作为包导入时（pytest 从仓库根收集）
    from scripts import forward_store as store  # noqa: E402

MARKET_TZ = ZoneInfo("Asia/Shanghai")

DEFAULT_DB = BACKEND / "data" / "forward_research.db"
DEFAULT_MANIFEST = BACKEND / "data" / "c0_universe_b1_verified.json"
DEFAULT_CALENDAR_DB = BACKEND / "data" / "fupan.db"
DEFAULT_SNAPSHOT_DIR = BACKEND / "data" / "forward_c0"
DEFAULT_ALLOWED_ROOT = BACKEND / "data"

#: 冻结清单版本与资格哈希（换清单必须同时改这两个常量并说明原因）
MANIFEST_VERSION = "b1v-2026-11-11"
MANIFEST_ELIGIBILITY_SHA256 = (
    "89434178ad134e7cff56ab09a2637dcd042683825361ad7a1f6ca08e5dcf8567"
)

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
#: 正式闸门不允许低于该值（防止用参数把覆盖率检查关掉）
MIN_UNIVERSE_COVERAGE_FLOOR = 0.95
MIN_BOARD_COVERAGE = 0.95
MIN_BOARD_ROWS = 20
READY_AT = dt_time(15, 30)
REQUIRED_COLUMNS = ("ts_code", "open", "high", "low", "close", "pct_chg", "vol", "amount")

STATUS_OK = "ok"
STATUS_NO_QUOTE = "no_quote_unverified"
STATUS_SUSPENDED = "suspended_confirmed"
STATUS_BATCH_FAILED = "batch_failed"

ALLOWED_TABLES = {
    "stock_daily",
    "trade_calendar",
    "data_revision",
    "ingest_log",
    "universe_status",
    "sqlite_sequence",
}

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_ALREADY = 2
EXIT_API_ERROR = 3
EXIT_DB_ERROR = 4


@dataclass(frozen=True)
class Manifest:
    """冻结研究范围（只用可前瞻且身份已核验的证券）。"""

    version: str
    eligibility_sha256: str
    universe_sha256: str
    data_cutoff: str | None
    codes: frozenset[str]
    board_counts: dict[str, int]
    total: int


@dataclass
class Batch:
    """一次取数的规范化结果（纯内存，未落库）。"""

    trade_date: date
    rows: list[dict] = field(default_factory=list)
    statuses: list[tuple[str, str, str]] = field(default_factory=list)
    by_board: dict[str, int] = field(default_factory=dict)
    by_board_eligible: dict[str, int] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)
    data_version: str = ""
    digest: str = ""


class BatchRejected(Exception):
    """整批拒绝。`error_type` 用于 ingest_log。"""

    def __init__(self, message: str, *, error_type: str = "validation", problems: list[str] | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.problems = problems or []


# ---------------------------------------------------------------------------
# 纯函数：规范化与校验
# ---------------------------------------------------------------------------


def _to_float(value) -> float | None:
    if value is None or isinstance(value, bool):
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


def batch_data_version(day: date, digest: str, revision: int) -> str:
    """数据版本：内容哈希 + 当日修订号，修订后版本必然变化。"""
    return f"tushare-daily:{day.isoformat()}:{digest[:12]}:r{revision}"


def load_manifest(
    path: Path,
    *,
    expect_version: str = MANIFEST_VERSION,
    expect_eligibility_sha256: str = MANIFEST_ELIGIBILITY_SHA256,
) -> Manifest:
    """读取并校验冻结清单：版本、资格哈希、全量哈希、资格标记都必须自洽。"""
    if not path.is_file():
        raise SystemExit(f"没有冻结清单 {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    version = str(payload.get("version") or "")
    if not version:
        raise SystemExit("冻结清单缺少 version")
    if expect_version and version != expect_version:
        raise SystemExit(f"清单版本 {version} ≠ 期望 {expect_version}：拒绝使用未知版本")

    securities = payload.get("securities")
    if not isinstance(securities, list) or not securities:
        raise SystemExit("冻结清单 securities 为空或格式错误")
    ordered = sorted(securities, key=lambda item: item["code"])

    eligibility = "\n".join(
        f"{item['code']}|{int(bool(item.get('forward_eligible')))}" for item in ordered
    )
    eligibility_sha = hashlib.sha256(eligibility.encode("utf-8")).hexdigest()
    if eligibility_sha != payload.get("eligibility_sha256"):
        raise SystemExit("资格哈希与清单内容不一致：清单可能被手工改动")
    if expect_eligibility_sha256 and eligibility_sha != expect_eligibility_sha256:
        raise SystemExit(
            f"资格哈希 {eligibility_sha[:12]}… ≠ 期望 {expect_eligibility_sha256[:12]}…：拒绝未知清单"
        )

    universe = "\n".join(
        f"{item['code']}|{item.get('name')}|{item['board']}" for item in ordered
    )
    if hashlib.sha256(universe.encode("utf-8")).hexdigest() != payload.get("universe_sha256"):
        raise SystemExit("全量哈希与清单内容不一致：清单可能被手工改动")

    counts = payload.get("counts") or {}
    if int(counts.get("total", -1)) != len(ordered):
        raise SystemExit("清单 counts.total 与实际条目数不一致")
    eligible = [item for item in ordered if item.get("forward_eligible")]
    if int(counts.get("forward_eligible", -1)) != len(eligible):
        raise SystemExit("清单 counts.forward_eligible 与实际可前瞻条目数不一致")
    if any(not item.get("identity_verified") for item in eligible):
        raise SystemExit("可前瞻证券里存在身份未核验的条目：拒绝使用")

    board_counts: dict[str, int] = {}
    for item in eligible:
        board_counts[item["board"]] = board_counts.get(item["board"], 0) + 1

    return Manifest(
        version=version,
        eligibility_sha256=eligibility_sha,
        universe_sha256=str(payload.get("universe_sha256")),
        data_cutoff=payload.get("data_cutoff"),
        codes=frozenset(item["code"] for item in eligible),
        board_counts=board_counts,
        total=len(eligible),
    )


def load_suspended_evidence(path: Path, manifest: Manifest) -> dict[str, str]:
    """可信停牌证据：`{"securities":[{"code":..., "reason":...}]}`；只允许清单内代码。"""
    if not path.is_file():
        raise SystemExit(f"没有停牌证据文件 {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload.get("securities") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise SystemExit("停牌证据格式错误：需要 securities 列表")
    result: dict[str, str] = {}
    for item in items:
        code = str(item.get("code") or "")
        reason = str(item.get("reason") or "").strip()
        if code not in manifest.codes:
            raise SystemExit(f"停牌证据里的 {code} 不在冻结清单内")
        if not reason:
            raise SystemExit(f"停牌证据 {code} 缺少 reason")
        result[code] = reason
    return result


def normalize_frame(
    raw_rows: list[dict],
    day: date,
    *,
    manifest: Manifest,
    ingested_at: str,
    data_version: str,
    suspended_reasons: dict[str, str] | None = None,
) -> Batch:
    """把 Tushare 原始行转成研究行；任何字段/单位问题 → 整批 `BatchRejected`。"""
    if not raw_rows:
        raise BatchRejected("接口返回空结果（不能解释为全市场停牌）", error_type="empty")
    if not set(REQUIRED_COLUMNS).issubset(raw_rows[0]):
        missing = sorted(set(REQUIRED_COLUMNS) - set(raw_rows[0]))
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
            problems.append(
                f"{code} 成交量/成交额必须为正（vol={values['vol']}, amount={values['amount']}）"
            )
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

    evidence = dict(suspended_reasons or {})
    codes = {row["code"] for row in rows}
    missing = sorted(manifest.codes - codes)
    returned_evidenced = sorted(set(evidence) & codes)
    if returned_evidenced:
        raise BatchRejected(
            f"停牌证据与行情冲突：{returned_evidenced[:10]} 当日有行情但被标为停牌",
            error_type="suspended_conflict",
        )
    unknown = sorted(set(evidence) - manifest.codes)
    if unknown:
        raise BatchRejected(f"停牌证据含清单外代码：{unknown[:10]}", error_type="suspended_conflict")

    missing_set = set(missing)
    statuses = []
    for code in sorted(manifest.codes):
        if code not in missing_set:
            statuses.append((code, STATUS_OK, ""))
        elif code in evidence:
            statuses.append((code, STATUS_SUSPENDED, evidence[code]))
        else:
            statuses.append((code, STATUS_NO_QUOTE, "absent_from_daily"))

    by_board: dict[str, int] = {}
    by_board_eligible: dict[str, int] = {}
    for row in rows:
        by_board[row["board"]] = by_board.get(row["board"], 0) + 1
        if row["code"] in manifest.codes:
            by_board_eligible[row["board"]] = by_board_eligible.get(row["board"], 0) + 1
    extras = sorted(codes - manifest.codes)
    digest = batch_digest(rows)
    return Batch(
        trade_date=day,
        rows=rows,
        statuses=statuses,
        by_board=by_board,
        by_board_eligible=by_board_eligible,
        missing=missing,
        extras=extras,
        data_version=data_version,
        digest=digest,
    )


def validate_batch(
    batch: Batch,
    *,
    manifest: Manifest,
    previous_board: dict[str, int] | None = None,
    min_universe_coverage: float = MIN_UNIVERSE_COVERAGE,
) -> list[str]:
    """整批完整性校验；返回问题列表（空 = 可提交）。"""
    if min_universe_coverage < MIN_UNIVERSE_COVERAGE_FLOOR:
        raise BatchRejected(
            f"覆盖率阈值 {min_universe_coverage:.1%} 低于硬下限 "
            f"{MIN_UNIVERSE_COVERAGE_FLOOR:.0%}：不允许绕过正式闸门",
            error_type="config",
        )
    problems: list[str] = []
    rows = len(batch.rows)
    if rows == 0:
        return ["当日没有任何有效行情行"]
    if rows >= MAX_ROWS:
        problems.append(f"返回 {rows} 行，达到 {MAX_ROWS} 上限：疑似触顶，必须分段/拒绝")
    unique = {row["code"] for row in batch.rows}
    if len(unique) != rows:
        problems.append(f"唯一代码 {len(unique)} != 行数 {rows}：存在重复行")

    matched = len(unique & manifest.codes)
    coverage = matched / manifest.total if manifest.total else 0.0
    if coverage < min_universe_coverage:
        problems.append(
            f"研究清单覆盖 {matched}/{manifest.total} = {coverage:.2%}"
            f"（< {min_universe_coverage:.1%}）：疑似整段证券缺失"
        )

    baseline = previous_board if previous_board else manifest.board_counts
    label = "上一确认日" if previous_board else "冻结清单"
    if not baseline:
        problems.append("没有分市场基线：拒绝仅凭总覆盖率放行")
    for board, expected in baseline.items():
        if expected < MIN_BOARD_ROWS:
            continue
        today_rows = batch.by_board_eligible.get(board, 0)
        ratio = today_rows / expected
        if ratio < MIN_BOARD_COVERAGE:
            problems.append(
                f"{board} 覆盖 {today_rows} 行，{label} {expected} 行，仅 {ratio:.1%}"
                f"（< {MIN_BOARD_COVERAGE:.0%}）：疑似该市场被静默丢弃"
            )
    return problems


# ---------------------------------------------------------------------------
# 目标库与路径保护
# ---------------------------------------------------------------------------


def _norm(path: Path) -> str:
    return os.path.normcase(str(path))


def _check_target(path: Path, *, allowed_root: Path = DEFAULT_ALLOWED_ROOT) -> Path:
    """只允许写 `<allowed_root>/forward_research.db`，并拒绝别名/链接/非前瞻库。"""
    raw = Path(path).expanduser()
    if raw.name != "forward_research.db":
        raise SystemExit(f"目标库名必须是 forward_research.db（当前 {raw.name}）")

    root = Path(allowed_root).expanduser().resolve(strict=False)
    resolved = raw.resolve(strict=False)
    if _norm(resolved.parent) != _norm(root):
        raise SystemExit(f"目标库必须位于 {root} 直下，实际解析为 {resolved}")

    if raw.is_symlink() or resolved.is_symlink():
        raise SystemExit("拒绝写入符号链接目标")
    if resolved.exists():
        if not resolved.is_file():
            raise SystemExit(f"目标不是普通文件：{resolved}")
        stat = resolved.stat()
        if stat.st_nlink > 1:
            raise SystemExit("拒绝写入硬链接目标（可能指向其他库）")
        if stat.st_size > 0:
            with resolved.open("rb") as handle:
                header = handle.read(16)
            if header != b"SQLite format 3\x00":
                raise SystemExit("目标文件不是 SQLite 数据库")
            con = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
            try:
                tables = {
                    row[0]
                    for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
            finally:
                con.close()
            unexpected = sorted(tables - ALLOWED_TABLES)
            if unexpected:
                raise SystemExit(
                    f"目标文件包含非前瞻库表 {unexpected}：拒绝把业务库改名为 forward_research.db"
                )
    else:
        if not resolved.parent.is_dir():
            raise SystemExit(f"目标目录不存在：{resolved.parent}")
    return resolved


# ---------------------------------------------------------------------------
# 数据库：前置条件与单事务写入
# ---------------------------------------------------------------------------


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


def connect_target(path: Path, *, create: bool):
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


def day_revision_readonly(db_path: Path, day: date) -> int | None:
    """读取当日完成标记；**库损坏必须失败关闭**，不能当成"未采集"。"""
    if not db_path.exists():
        return None
    if db_path.stat().st_size == 0:
        return None
    engine = store.connect_readonly(db_path)
    try:
        with engine.connect() as conn:
            return _day_revision(conn, day)
    except Exception as error:  # noqa: BLE001
        raise BatchRejected(
            f"无法读取目标库完成标记（疑似损坏）：{error}", error_type="db_unreadable"
        ) from error
    finally:
        engine.dispose()


def _calendar_ready(db_path: Path, day: date) -> tuple[bool, str]:
    if not db_path.exists() or db_path.stat().st_size == 0:
        return False, "目标库还没有交易日历：先运行 --init-calendar"
    engine = store.connect_readonly(db_path)
    try:
        with engine.connect() as conn:
            row = conn.exec_driver_sql(
                "SELECT 1 FROM trade_calendar WHERE trade_date=?", (day.isoformat(),)
            ).fetchone()
            return (bool(row), "" if row else f"目标库交易日历不含 {day}")
    except Exception as error:  # noqa: BLE001
        raise BatchRejected(
            f"无法读取目标库交易日历（疑似损坏）：{error}", error_type="db_unreadable"
        ) from error
    finally:
        engine.dispose()


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
    for (code,) in conn.exec_driver_sql(
        "SELECT code FROM stock_daily WHERE trade_date=?", (previous,)
    ):
        board = store.board_of(str(code))
        counts[board] = counts.get(board, 0) + 1
    return counts


def _append_ingest(conn, day: date, *, status: str, rows: int | None, message: dict, now: datetime) -> None:
    conn.exec_driver_sql(
        "INSERT INTO ingest_log(trade_date, status, rows, message, created_at) VALUES (?,?,?,?,?)",
        (day.isoformat(), status, rows, json.dumps(message, ensure_ascii=False), now.isoformat()),
    )


def _tick(failpoint: str | None, stage: str) -> None:
    if failpoint == stage:
        raise RuntimeError(f"injected failure: {stage}")


def _write_confirmed(
    engine,
    batch: Batch,
    *,
    now: datetime,
    revise: bool,
    expected_revision: int,
    message: dict,
    _failpoint: str | None = None,
) -> None:
    """单事务：行情 + 逐券状态 + 完成标记 + 成功日志，一起提交或一起回滚。"""
    day_text = batch.trade_date.isoformat()
    with engine.begin() as conn:
        existing = _day_revision(conn, batch.trade_date)
        if (existing or 0) != expected_revision - 1:
            raise RuntimeError(
                f"当日修订号并发变化：库内 {existing}，期望 {expected_revision - 1}"
            )
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
        _tick(_failpoint, "rows")

        for code, status, reason in batch.statuses:
            conn.exec_driver_sql(
                "INSERT INTO universe_status(trade_date, code, status, reason) VALUES (?,?,?,?)",
                (day_text, code, status, reason),
            )
        _tick(_failpoint, "status")

        written = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM stock_daily WHERE trade_date=?", (day_text,)
        ).scalar()
        if int(written or 0) != len(batch.rows):
            raise RuntimeError(f"事务内行数校验失败：写 {written}，应为 {len(batch.rows)}")

        for domain in (f"stock_daily:{day_text}", "stock_daily"):
            conn.exec_driver_sql(
                """
                INSERT INTO data_revision(domain, revision, updated_at) VALUES (?,?,?)
                ON CONFLICT(domain) DO UPDATE SET revision=revision+1, updated_at=excluded.updated_at
                """,
                (domain, expected_revision, now.isoformat()),
            )
        _tick(_failpoint, "revision")

        _append_ingest(conn, batch.trade_date, status="ok", rows=len(batch.rows), message=message, now=now)
        _tick(_failpoint, "ok_log")
        _tick(_failpoint, "before_commit")


# ---------------------------------------------------------------------------
# 冻结快照关联（修订保护）
# ---------------------------------------------------------------------------


def _current_revision(db_path: Path, day: date) -> int | None:
    return day_revision_readonly(db_path, day)


def _frozen_snapshot_problem(
    snapshot_dir: Path, day: date, db_revision: int | None
) -> str | None:
    """有正式冻结快照时返回原因；没有则返回 None。损坏一律返回原因（失败关闭）。"""
    try:
        from scripts.forward_track_c0 import (
            MODE_FORWARD,
            STATE_COMPLETE,
            STATE_CORRUPTED,
            STATE_INCOMPLETE,
            STATE_NONE,
            inspect_snapshot,
        )
    except Exception as error:  # noqa: BLE001
        return f"无法加载快照校验器（{error}）：按已冻结处理"
    state = inspect_snapshot(Path(snapshot_dir), day, MODE_FORWARD)
    if state.state == STATE_NONE:
        return None
    if state.state in (STATE_INCOMPLETE, STATE_CORRUPTED):
        return f"同日冻结快照状态异常（{state.state}：{state.detail}），拒绝修订"
    if state.state != STATE_COMPLETE:
        return f"同日冻结快照状态未知（{state.state}），拒绝修订"
    meta = state.meta or {}
    if meta.get("signal_date") != day.isoformat():
        return "冻结快照的 signal_date 与目标日期不一致"
    revision = meta.get("source_data_revision") or {}
    value = revision.get("value") if isinstance(revision, dict) else None
    recorded = value.get(f"stock_daily:{day.isoformat()}") if isinstance(value, dict) else None
    if recorded is None or db_revision is None or int(recorded) != int(db_revision):
        return (
            f"冻结快照记录的数据版本（{recorded}）与库内当日版本（{db_revision}）不一致"
            "或缺失：拒绝修订"
        )
    return "同日已生成 FORWARD_FROZEN 快照：原始输入不得覆盖"


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
    *,
    purpose: str,
    revise: bool,
) -> None:
    """失败留痕；留痕本身失败也不能掩盖原始错误。"""
    try:
        engine = connect_target(db_path, create=True)
        try:
            with engine.begin() as conn:
                _append_ingest(
                    conn, day, status="failed", rows=None,
                    message={
                        "error_type": error_type,
                        "error": _safe(message)[:1000],
                        "problems": problems[:20],
                        "elapsed_ms": elapsed_ms,
                        "request_date": day.isoformat(),
                        "purpose": purpose,
                        "revised": bool(revise),
                        "status": STATUS_BATCH_FAILED,
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
    manifest: Manifest,
    calendar: list[date],
    now: datetime,
    execute: bool,
    revise: bool = False,
    revise_reason: str = "",
    suspended_reasons: dict[str, str] | None = None,
    snapshot_dir: Path = DEFAULT_SNAPSHOT_DIR,
    purpose: str = "live",
    min_universe_coverage: float = MIN_UNIVERSE_COVERAGE,
    allowed_root: Path = DEFAULT_ALLOWED_ROOT,
) -> dict:
    """执行/预演一天。`fetch(day) -> list[dict]` 由调用方注入。"""
    db_path = _check_target(db_path, allowed_root=allowed_root)
    if purpose not in ("live", "backfill"):
        raise BatchRejected(f"未知 purpose={purpose}", error_type="config")
    if min_universe_coverage < MIN_UNIVERSE_COVERAGE_FLOOR:
        raise BatchRejected(
            f"覆盖率阈值 {min_universe_coverage:.1%} 低于硬下限 "
            f"{MIN_UNIVERSE_COVERAGE_FLOOR:.0%}",
            error_type="config",
        )
    if now.tzinfo is None:
        raise ValueError("now 必须带时区")
    local = now.astimezone(MARKET_TZ)
    today = local.date()
    if day > today:
        raise BatchRejected(f"{day} 晚于市场当前日期 {today}：拒绝采集未来日期", error_type="date")
    if purpose == "live" and day != today:
        raise BatchRejected(
            f"live 采集只允许当日（{today}），{day} 属于历史补采，请用 --purpose backfill",
            error_type="purpose",
        )
    if purpose == "backfill" and day >= today:
        raise BatchRejected(
            f"backfill 只允许历史日期（< {today}），{day} 不能冒充预热", error_type="purpose"
        )
    if day not in calendar:
        raise BatchRejected(f"{day} 不在交易日历里，不是有效交易日", error_type="calendar")
    ready_at = datetime.combine(day, READY_AT, tzinfo=MARKET_TZ)
    if local < ready_at:
        raise BatchRejected(
            f"当前 {local:%Y-%m-%d %H:%M} 早于 {day} {READY_AT:%H:%M}：收盘数据未就绪",
            error_type="not_ready",
        )

    revision = _current_revision(db_path, day)
    if revision is not None and not revise:
        return {"status": "already_confirmed", "exit_code": EXIT_ALREADY, "day": day.isoformat()}
    if revise:
        if not revise_reason.strip():
            raise BatchRejected("--revise 必须同时给出 --revise-reason", error_type="config")
        if revision is None:
            raise BatchRejected(f"{day} 还没有确认记录，没有可修订的对象", error_type="revise")
        problem = _frozen_snapshot_problem(Path(snapshot_dir), day, revision)
        if problem:
            raise BatchRejected(problem, error_type="frozen")
    expected_revision = (revision or 0) + 1

    ready, detail = _calendar_ready(db_path, day)

    if not execute:
        return {
            "status": "plan",
            "exit_code": EXIT_OK,
            "day": day.isoformat(),
            "purpose": purpose,
            "manifest_version": manifest.version,
            "manifest": manifest.total,
            "next_revision": expected_revision,
            "db_exists": db_path.is_file(),
            "calendar_ready": ready,
            "calendar_detail": detail,
            "frozen": bool(_frozen_snapshot_problem(Path(snapshot_dir), day, revision)) if revision else False,
        }
    if not ready:
        raise BatchRejected(detail, error_type="calendar")

    started = time.monotonic()
    ingested_at = local.isoformat()
    try:
        raw = fetch(day)
    except Exception as error:  # noqa: BLE001
        elapsed_ms = int((time.monotonic() - started) * 1000)
        _safe_record_failure(
            db_path, day, "api", str(error), [], elapsed_ms, local,
            purpose=purpose, revise=revise,
        )
        raise BatchRejected(f"接口调用失败（未重试）：{_safe(str(error))}", error_type="api") from error

    elapsed_ms = int((time.monotonic() - started) * 1000)
    try:
        batch = normalize_frame(
            raw, day,
            manifest=manifest,
            ingested_at=ingested_at,
            data_version="pending",
            suspended_reasons=suspended_reasons,
        )
        batch.data_version = batch_data_version(day, batch.digest, expected_revision)
        for row in batch.rows:
            row["data_version"] = batch.data_version

        engine = connect_target(db_path, create=True)
        try:
            with engine.connect() as conn:
                previous_board = _previous_board_counts(conn, day)
            problems = validate_batch(
                batch,
                manifest=manifest,
                previous_board=previous_board,
                min_universe_coverage=min_universe_coverage,
            )
            if problems:
                raise BatchRejected("完整性校验未通过", error_type="incomplete", problems=problems)
            message = {
                "rows": len(batch.rows),
                "unique": len({row['code'] for row in batch.rows}),
                "by_board": batch.by_board,
                "by_board_eligible": batch.by_board_eligible,
                "missing": len(batch.missing),
                "missing_sample": batch.missing[:20],
                "extras": len(batch.extras),
                "extras_sample": batch.extras[:20],
                "elapsed_ms": elapsed_ms,
                "request_date": day.isoformat(),
                "source": "tushare",
                "data_version": batch.data_version,
                "manifest_version": manifest.version,
                "manifest_eligibility_sha256": manifest.eligibility_sha256,
                "purpose": purpose,
                "revised": bool(revise),
                "revise_reason": revise_reason if revise else "",
                "revision": expected_revision,
            }
            _write_confirmed(
                engine, batch,
                now=local,
                revise=revise,
                expected_revision=expected_revision,
                message=message,
            )
        finally:
            engine.dispose()
    except BatchRejected as error:
        _safe_record_failure(
            db_path, day, error.error_type, str(error), error.problems, elapsed_ms, local,
            purpose=purpose, revise=revise,
        )
        raise
    except Exception as error:  # noqa: BLE001 - 写库/未知错误：先留痕再向上抛
        _safe_record_failure(
            db_path, day, "db", str(error), [], elapsed_ms, local,
            purpose=purpose, revise=revise,
        )
        raise

    return {
        "status": "confirmed",
        "exit_code": EXIT_OK,
        "day": day.isoformat(),
        "rows": len(batch.rows),
        "by_board": batch.by_board,
        "missing": len(batch.missing),
        "extras": len(batch.extras),
        "data_version": batch.data_version,
        "revision": expected_revision,
        "elapsed_ms": elapsed_ms,
    }


# ---------------------------------------------------------------------------
# 交易日历初始化（只写目标库）
# ---------------------------------------------------------------------------


def init_calendar(
    db_path: Path,
    *,
    source_db: Path,
    start: date,
    end: date,
    now: datetime,
    allowed_root: Path = DEFAULT_ALLOWED_ROOT,
) -> dict:
    """把本地可信日历复制到独立研究库；不联网、不改生产日历。"""
    db_path = _check_target(db_path, allowed_root=allowed_root)
    if now.tzinfo is None:
        raise ValueError("now 必须带时区")
    today = now.astimezone(MARKET_TZ).date()
    if end < today:
        raise BatchRejected(f"日历终点 {end} 早于当前市场日期 {today}", error_type="calendar")
    if start > end:
        raise BatchRejected("日历起点晚于终点", error_type="calendar")
    if not source_db.is_file():
        raise SystemExit(f"没有交易日历库 {source_db}")
    con = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)
    try:
        days = [
            date.fromisoformat(row[0])
            for row in con.execute(
                "SELECT trade_date FROM trade_calendar WHERE trade_date BETWEEN ? AND ? "
                "ORDER BY trade_date",
                (start.isoformat(), end.isoformat()),
            )
        ]
    finally:
        con.close()
    if not days:
        raise BatchRejected("源日历在给定区间内没有交易日", error_type="calendar")
    if not any(item <= today for item in days):
        raise BatchRejected(f"日历未覆盖最近交易日（≤ {today}）", error_type="calendar")
    if max(days) <= today:
        raise BatchRejected(
            f"日历没有覆盖下一交易日（最大 {max(days)} ≤ {today}）", error_type="calendar"
        )

    engine = connect_target(db_path, create=True)
    try:
        with engine.begin() as conn:
            for item in days:
                conn.exec_driver_sql(
                    "INSERT OR IGNORE INTO trade_calendar(trade_date) VALUES (?)", (item.isoformat(),)
                )
            stored = int(
                conn.exec_driver_sql("SELECT COUNT(*) FROM trade_calendar").scalar() or 0
            )
    finally:
        engine.dispose()
    return {
        "status": "calendar_ready",
        "exit_code": EXIT_OK,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": len(days),
        "next_trade_day": min(item for item in days if item > today).isoformat(),
        "stored": stored,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _token() -> str:
    try:
        from scripts.verify_tushare_daily import load_token

        return load_token()
    except Exception:  # noqa: BLE001
        return os.environ.get("TUSHARE_TOKEN", "").strip()


def _safe(text: str) -> str:
    token = _token()
    return text.replace(token, "***") if token else text


def _fetch_tushare(day: date):
    token = _token()
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
    parser.add_argument("--date", default="", help="交易日 YYYY-MM-DD")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--calendar-db", default=str(DEFAULT_CALENDAR_DB))
    parser.add_argument("--snapshot-dir", default=str(DEFAULT_SNAPSHOT_DIR))
    parser.add_argument("--execute", action="store_true", help="真正发送一次请求并写库")
    parser.add_argument("--purpose", choices=("live", "backfill"), default="live")
    parser.add_argument("--revise", action="store_true", help="仅修订未冻结日期；必须给原因")
    parser.add_argument("--revise-reason", default="")
    parser.add_argument("--suspended-file", default="", help="可信停牌证据 JSON")
    parser.add_argument("--min-universe-coverage", type=float, default=MIN_UNIVERSE_COVERAGE)
    parser.add_argument("--init-calendar", action="store_true", help="初始化目标库交易日历后退出")
    parser.add_argument("--calendar-start", default="")
    parser.add_argument("--calendar-end", default="")
    parser.add_argument("--expect-manifest-version", default=MANIFEST_VERSION)
    parser.add_argument("--expect-eligibility-sha256", default=MANIFEST_ELIGIBILITY_SHA256)
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    now = datetime.now(MARKET_TZ)

    if args.init_calendar:
        start = date.fromisoformat(args.calendar_start) if args.calendar_start else now.date() - timedelta(days=90)
        end = date.fromisoformat(args.calendar_end) if args.calendar_end else now.date() + timedelta(days=30)
        try:
            result = init_calendar(
                db_path, source_db=Path(args.calendar_db), start=start, end=end, now=now
            )
        except BatchRejected as error:
            print(f"拒绝初始化日历：{error}（error_type={error.error_type}）")
            return EXIT_REJECTED
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return EXIT_OK

    if not args.date:
        parser.error("采集模式需要 --date（或使用 --init-calendar）")
    day = date.fromisoformat(args.date)
    manifest = load_manifest(
        Path(args.manifest),
        expect_version=args.expect_manifest_version,
        expect_eligibility_sha256=args.expect_eligibility_sha256,
    )
    calendar = load_calendar(Path(args.calendar_db), day)
    suspended = (
        load_suspended_evidence(Path(args.suspended_file), manifest) if args.suspended_file else {}
    )

    if not args.execute:
        print(
            f"计划：daily(trade_date={day:%Y%m%d})；清单 {manifest.version} "
            f"{manifest.total} 只；日历{'包含' if day in calendar else '不包含'}该日；"
            f"目标 {db_path.name}；用途 {args.purpose}"
        )
        print("[dry-run] 未发送请求、未建库。")
        return EXIT_OK

    try:
        result = collect(
            db_path, day,
            fetch=_fetch_tushare,
            manifest=manifest,
            calendar=calendar,
            now=now,
            execute=True,
            revise=args.revise,
            revise_reason=args.revise_reason,
            suspended_reasons=suspended,
            snapshot_dir=Path(args.snapshot_dir),
            purpose=args.purpose,
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
