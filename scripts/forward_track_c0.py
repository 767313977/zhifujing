"""C0 策略的前瞻纸面跟踪（本地、手动、只读）。

## 两种模式，语义完全不同

| 模式 | 用途 | 文件名 | meta 标记 | 计入前瞻绩效 |
| --- | --- | --- | --- | --- |
| `replay` | 代码测试、与历史回测的一致性验证 | `replay_signals_YYYY-MM-DD.*` | `REPLAY_ONLY` | **否** |
| `forward` | 真实前瞻冻结 | `signals_YYYY-MM-DD.*` | `FORWARD_FROZEN` | 是 |

`forward` 会拒绝历史日期：用今天的库算 2026-10-19 的分明是复盘重放，不是前瞻观察。
把两者写进同一个文件名，才会出现「事后补一份 10 月 19 日的记录，冒充当时就发过信号」。

CLI 只用 `--mode replay` / `--mode forward` 两个取值，不再出现 `live` 这个别名。

## B1v：前瞻池恢复为历史全部 5,108 只

北交所 32 只已按北交所官方上市公司名录（上市日期）与《新旧代码对照表》逐只核验：
4 只为存量切换、28 只为 2024-04-22 之后以 920 直接上市，身份均可确认。
因此 `forward` 使用 B1v 清单（`backend/data/c0_universe_b1_verified.json`），
与历史 C0 的证券集合完全一致；旧版 B1 清单与哈希保留不覆盖，仅作历史证据。

`forward` 仍只使用清单内 `forward_eligible=true` 的证券，缺失清单时直接拒绝，
不按代码段临时拼池；`replay` 不受清单影响。

## `forward` 的启动闸门（全部通过才落盘）

1. D 在交易日历里（`trade_calendar` 表，缺失即拒绝，不猜）；
2. 当前时间 ≥ D 日 15:30（**最早可冻结时点**；数据实际完成可以更晚，
   只要还在下一条窗口内）；
3. 当前时间 < 下一交易日 09:15（集合竞价前）——2、3 合起来就是合法冻结窗口；
4. 时区固定 `Asia/Shanghai`；
5. 库里最新交易日 == D（没有当日数据就拒绝，不自动下载）；
6. D 日行数 ≥ 近 5 个交易日行数中位数 × 95%；
7. D 日 OHLC/pct_chg 缺失率 ≤ 0.5%，amount 缺失率 ≤ 0.5%；
8. 候选池三因子缺失率 ≤ 2%；
9. 分市场覆盖：沪主板 / 科创板 / 深主板 / 创业板 / 北交所各自的当日行数
   ≥ 各自近 5 日中位数 × 95%（总行数接近 ≠ 证券范围完整，防止某市场被静默丢弃）。

任何一条不满足 → 打印原因并退出码 3，**不生成任何文件**。当前 `history.db`
最新只到 2026-10-19，所以正式前瞻运行必然被拒绝 —— 这是预期行为。

## 快照的五种状态

一次冻结要落两个文件，中间崩溃就会留下「半套」。工具显式区分：

| JSONL | meta | 状态 | 处理 |
| --- | --- | --- | --- |
| 不存在 | 不存在 | `NONE` | 允许创建 |
| 存在 | 存在 | `COMPLETE` | 校验哈希/模式/版本；拒绝覆盖 |
| 存在 | 不存在 | `INCOMPLETE_PUBLISH` | 拒绝当作有效快照；不自动删除 |
| 不存在 | 存在 | `INCOMPLETE_PUBLISH` | 同上 |
| 任一校验失败 | 任意 | `CORRUPTED_SNAPSHOT` | 拒绝评价 |

## 这个工具不做什么

- 不建定时任务、不连网络、不连交易账户、不写业务库、不下单。
- 不自动下载行情、不连生产库。
- 不修改生产权重，不改生产选股逻辑。

## 完整性说明（不要夸大）

`content_sha256` 只能校验「JSONL 内容」与「meta 里记录的哈希」是否自洽 ——
用来发现**单文件**被改、或写入中断。它**不能**证明创建时间，也**不能**防篡改：
同时替换 JSONL 和 meta 就能伪造一份自洽的记录。真正的「从未被改过」需要独立
时间戳或外部只追加留痕（RFC 3161、对象存储 WORM 等），本轮不引入外部服务。

并发与崩溃保护只做两件事：`os.link` 独占发布（已存在就失败，绝不覆盖）、
先写临时文件再发布（崩溃只会留下 `.tmp-*`，不会被当成有效快照）。
`os.link` 在个别文件系统上不可用时退化为 `O_EXCL` 独占创建 + 复制，
仍是「先到者胜」，只是不再原子。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
# 直接 `python scripts/forward_track_c0.py` 时 sys.path[0] 是 scripts/，
# 不补 ROOT 就 import 不到 `scripts.*`（用 -m 跑才碰巧能找到）。
for _path in (ROOT, BACKEND):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from sqlalchemy import create_engine, func, or_, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import StockDaily, TradeCalendar  # noqa: E402
from app.services import trading_days  # noqa: E402
from app.services.research_backtest import (  # noqa: E402
    DEFAULT_ENTRY_DELAY,
    DEFAULT_EXIT_DELAY,
    Benchmark,
    Signal,
    load_matrix,
    run_signals,
)
from scripts.backtest_reversal import (  # noqa: E402
    MIN_POOL,
    _code_columns,
    _day_candidates,
    _net,
    _rank_top,
)
from scripts.forward_store import DATA_PROTOCOL_VERSION  # noqa: E402

#: 评价器版本：只影响评价展示/审计严格度，不改变信号与成交规则。
EVALUATOR_VERSION = "forward-evaluator-v1"

logger = logging.getLogger("forward_c0")

HISTORY_DB = BACKEND / "data" / "history.db"
OUT_DIR = BACKEND / "data" / "forward_c0"

MARKET_TZ = ZoneInfo("Asia/Shanghai")

STRATEGY_VERSION = "c0-2026-11-11-b1v"
TOP_N = 20
HORIZON = 5
COSTS = (30.0, 50.0, 100.0)

#: 正式评价需要审计的后续交易日数：入场延迟 + 持有期 + 退出延迟。
#: 5 + (5 - 1) + 10 = 19，与交易引擎默认常量同源，避免两处硬编码漂移。
AUDIT_TRADING_DAYS = DEFAULT_ENTRY_DELAY + (HORIZON - 1) + DEFAULT_EXIT_DELAY

#: B1v 证券清单：历史池全部证券（北交所 32 只已按官方名录核验）
UNIVERSE_FILE = BACKEND / "data" / "c0_universe_b1_verified.json"

#: 与 `backtest_reversal.build_signals` 的「当日有效池 < 30 跳过」共用同一阈值（见导入）

#: forward 闸门参数
DATA_READY_AT = time(15, 30)
NEXT_OPEN_AT = time(9, 15)
MIN_COVERAGE_RATIO = 0.95
MAX_NULL_RATIO = 0.005
MAX_FACTOR_MISSING_RATIO = 0.02
#: 分市场覆盖检查：近 5 日中位数有这么多只才比较（太小的板块不因停牌抖动误报）
MIN_BOARD_ROWS = 20

BOARD_NAMES = ("沪主板", "科创板", "深主板", "创业板", "北交所", "其他")

MODE_FORWARD = "forward"
MODE_REPLAY = "replay"
MODE_LABELS = {MODE_FORWARD: "FORWARD_FROZEN", MODE_REPLAY: "REPLAY_ONLY"}

#: 冻结协议 —— 任何一项变动都必须换 `STRATEGY_VERSION`，不能就地改
PROTOCOL: dict = {
    "strategy": "C0 原三因子（生产 factor_score 公式）",
    "weights": {"rev20": 0.30, "illiq20": 0.30, "amp20": 0.40},
    "window": 20,
    "min_coverage": 0.9,
    "universe_definition": (
        "B1v：历史研究库内出现过的全部证券（含主板/创业板/科创板/北交所）。"
        "北交所 32 只已按北交所官方上市公司名录与新旧代码对照表核验身份"
        "（4 只存量切换 + 28 只 920 直接上市）；含 ST（ST 不剔除）；"
        "不自动纳入历史集合以外的新证券；不套用生产流动性门槛与市值上限"
    ),
    "suspension_filter": "D 日无行情记录的代码不进入候选（不做长期停牌单独判定）",
    "liquidity_window": 20,
    "percentile_scope": "当日全体候选池内",
    "tie_break": "分数降序，同分按代码升序",
    "missing_factor": "单因子缺失按 0 分位（排最后），不剔除整只票",
    "top_n": TOP_N,
    "signal_time": "D 日收盘后",
    "entry": "D+1 开盘",
    "exit": "第 5 个交易日收盘（引擎交易日定义，跌停顺延）",
    "costs_bps": list(COSTS),
    "data_source": "backend/data/history.db（SQLite mode=ro）",
    "integrity": "content_sha256 只做本地自洽检查，不提供独立时间戳",
    "risk_flags": (
        "SURVIVORSHIP_BIAS",
        "ST_STATUS_PARTIAL",
    ),
}


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------


def _readonly_engine(path: Path):
    return create_engine(f"sqlite:///file:{path.as_posix()}?mode=ro&uri=true")


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(Path(__file__).resolve().parent.parent),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _data_revision(session: Session, db_path: Path) -> dict:
    """数据版本证据：优先 data_revision 表，否则用文件 mtime + size。"""
    try:
        from sqlalchemy import text

        pairs = session.execute(
            text("SELECT domain, revision FROM data_revision ORDER BY domain")
        ).all()
        if pairs:
            return {"kind": "data_revision", "value": {k: int(v) for k, v in pairs}}
    except Exception:  # noqa: BLE001
        session.rollback()
    stat = db_path.stat()
    return {
        "kind": "file_stat",
        "value": {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size},
    }


def revision_mismatch(meta: dict, current: dict, day: date) -> str | None:
    """校验快照记录的数据版本与库内当日版本一致（防止改写输入后仍被评价）。

    只有库内确有 `stock_daily:D` 版本时才强制比较；历史库（无 data_revision 表）
    保持原行为。快照缺记录或版本不同 → 返回原因，调用方必须拒绝评价。
    """
    domain = f"stock_daily:{day.isoformat()}"
    recorded_block = meta.get("source_data_revision") or {}
    recorded_value = recorded_block.get("value") if isinstance(recorded_block, dict) else None
    recorded = recorded_value.get(domain) if isinstance(recorded_value, dict) else None
    current_value = current.get("value") or {}
    if current.get("kind") != "data_revision":
        if recorded is not None:
            return "快照记录了 data_revision，但库内版本表缺失/不可读：拒绝评价（疑似绕过）"
        return None
    if recorded is None:
        if domain in current_value:
            return f"快照未记录 {domain} 的数据版本：无法证明输入未被改写，拒绝评价"
        return None
    if domain not in current_value:
        return f"库内缺少 {domain} 的完成标记：拒绝评价（疑似绕过）"
    if int(recorded) != int(current_value[domain]):
        return (
            f"{domain} 数据版本已变化（快照 {recorded}，当前 {current_value[domain]}）：拒绝评价"
        )
    return None


def day_audit_problem(session: Session, day: date, *, strict: bool = False) -> str | None:
    """审计某日是否具备可核验的采集证据（版本、成功日志、内容哈希）。

    `strict=True` 用于正式前瞻：**缺表、缺日志、版本不符都算失败**，不允许把
    采集器写入的库误判成历史库。`strict=False` 保持历史库兼容（返回 None）。
    """
    from sqlalchemy import text

    domain = f"stock_daily:{day.isoformat()}"
    try:
        revision_row = session.execute(
            text("SELECT revision FROM data_revision WHERE domain=:domain"), {"domain": domain}
        ).first()
    except Exception as error:  # noqa: BLE001 - 没有版本表
        session.rollback()
        if strict:
            return f"库内缺失 data_revision 表（{error}）：拒绝正式评价"
        return None
    if revision_row is None:
        if strict:
            return f"{day} 缺少当日完成标记 {domain}：拒绝正式评价"
        return None

    try:
        log_row = session.execute(
            text(
                "SELECT message FROM ingest_log WHERE trade_date=:day AND status='ok' "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"day": day.isoformat()},
        ).first()
    except Exception as error:  # noqa: BLE001 - 没有 ingest_log 表
        session.rollback()
        if strict:
            return f"库内缺失 ingest_log 表（{error}）：拒绝正式评价"
        return None
    if not log_row:
        if strict:
            return f"{day} 缺少成功采集日志：拒绝正式评价"
        return None
    try:
        message = json.loads(log_row[0])
    except Exception as error:  # noqa: BLE001
        return f"{day} 成功日志无法解析：拒绝评价（{error}）"
    version = str(message.get("data_version") or "")
    if not version.startswith("tushare-daily:"):
        if strict:
            return f"{day} 成功日志不是 Tushare 数据版本：拒绝正式评价"
        return None
    parts = version.split(":")
    if len(parts) < 4 or len(parts[2]) < 12:
        return f"{day} data_version 格式非法（{version}）：拒绝评价"
    expected = parts[2][:12]
    recorded_revision = message.get("revision")
    if strict and recorded_revision is not None and int(recorded_revision) != int(revision_row[0]):
        return (
            f"{day} 日志修订号 {recorded_revision} ≠ 当日完成标记 {revision_row[0]}："
            "拒绝正式评价"
        )

    from scripts.forward_collect_tushare import batch_digest

    rows = [
        {
            "code": item.code,
            "open": item.open,
            "high": item.high,
            "low": item.low,
            "close": item.close,
            "pct_chg": item.pct_chg,
            "volume": item.volume,
            "amount": item.amount,
        }
        for item in session.execute(
            select(
                StockDaily.code,
                StockDaily.open,
                StockDaily.high,
                StockDaily.low,
                StockDaily.close,
                StockDaily.pct_chg,
                StockDaily.volume,
                StockDaily.amount,
            ).where(StockDaily.trade_date == day)
        )
    ]
    if not rows:
        return f"{day} 行情为空：拒绝评价"
    try:
        actual = batch_digest(rows)[:12]
    except Exception as error:  # noqa: BLE001 - 字段被改成空值等
        return f"{day} 行情无法计算内容哈希：拒绝评价（{error}）"
    if actual != expected:
        return f"{day} 行情内容哈希 {actual} ≠ 采集记录 {expected}：库内行情被改动，拒绝评价"
    return None


def day_content_mismatch(session: Session, day: date) -> str | None:
    """历史库兼容入口：没有成功日志/版本表时返回 None（严格版见 `day_audit_problem`）。"""
    return day_audit_problem(session, day, strict=False)


def _forward_audit_problems(session: Session, day: date, meta: dict) -> list[str]:
    """正式前瞻（Tushare 源）评价前的完整审计：信号日 + 已存在的后续入场/退出日。"""
    source = meta.get("data_source") or {}
    provider = source.get("provider")
    if provider != "tushare":
        return [
            f"正式前瞻快照数据源 {provider!r} 不是 Tushare 前瞻协议"
            "（历史库/未知来源不得冒充正式前瞻）"
        ]
    problems: list[str] = []
    if source.get("data_protocol") != DATA_PROTOCOL_VERSION:
        problems.append(
            f"快照数据协议 {source.get('data_protocol')!r} ≠ 当前 {DATA_PROTOCOL_VERSION}"
        )
    if source.get("db") != "forward_research.db":
        problems.append(f"快照数据源 {source.get('db')!r} 不是独立前瞻库 forward_research.db")

    signal_problem = day_audit_problem(session, day, strict=True)
    if signal_problem:
        problems.append(signal_problem)

    from sqlalchemy import text

    try:
        later = [
            date.fromisoformat(row[0])
            for row in session.execute(
                text(
                    "SELECT trade_date FROM trade_calendar WHERE trade_date > :day "
                    "ORDER BY trade_date LIMIT :limit"
                ),
                {"day": day.isoformat(), "limit": AUDIT_TRADING_DAYS},
            )
        ]
    except Exception as error:  # noqa: BLE001 - 正式前瞻必须有日历
        session.rollback()
        problems.append(f"无法读取交易日历（{error}）：不能核对 D+1~D+{AUDIT_TRADING_DAYS} 依赖")
        return problems

    latest = session.scalar(select(func.max(StockDaily.trade_date)))
    for item in later:
        marker = session.execute(
            text("SELECT 1 FROM data_revision WHERE domain=:domain LIMIT 1"),
            {"domain": f"stock_daily:{item.isoformat()}"},
        ).first()
        exists = session.execute(
            text("SELECT 1 FROM stock_daily WHERE trade_date=:day LIMIT 1"),
            {"day": item.isoformat()},
        ).first()
        if not exists:
            if marker:
                problems.append(
                    f"{item} 已有完成标记却没有任何行情行：数据不自洽，拒绝正式评价"
                )
            elif latest is not None and item <= latest:
                problems.append(
                    f"{item} 已越过库内最新日 {latest}，却没有任何采集记录："
                    "依赖窗口存在缺口，拒绝将收益视为完整"
                )
            continue  # 尚未到达/尚未采集：保持 pending，不伪造收益
        problem = day_audit_problem(session, item, strict=True)
        if problem:
            problems.append(problem)
    return problems


def detect_data_source(session: Session, db_path: Path, day: date, revision: dict) -> dict:
    """识别数据来源与协议（写进快照 meta，评价时据此选择审计强度）。"""
    provider = "history"
    domain = f"stock_daily:{day.isoformat()}"
    values = revision.get("value") or {}
    if revision.get("kind") == "data_revision" and domain in values:
        try:
            from sqlalchemy import text

            sample = session.execute(
                text("SELECT data_version FROM stock_daily WHERE trade_date=:day LIMIT 1"),
                {"day": day.isoformat()},
            ).scalar()
        except Exception:  # noqa: BLE001
            session.rollback()
            sample = None
        if sample and str(sample).startswith("tushare-daily:"):
            provider = "tushare"
    if provider == "tushare":
        return {
            "provider": "tushare",
            "db": db_path.name,
            "data_protocol": DATA_PROTOCOL_VERSION,
            "revision_domain": domain,
            "units": {
                "raw": {"vol": "手", "amount": "千元"},
                "normalized": {"volume": "股", "amount": "元"},
            },
            "pct_source": "official",
        }
    return {
        "provider": "history",
        "db": db_path.name,
        "data_protocol": "history-derived-v1",
        "units": {
            "raw": {"volume": "股", "amount": "元"},
            "normalized": {"volume": "股", "amount": "元"},
        },
        "pct_source": "derived",
    }


def load_universe(path: Path) -> dict:
    """读取 B1 证券清单；缺失或不合法直接拒绝（不生成正式前瞻信号）。"""
    if not path.is_file():
        raise SystemExit(
            f"缺少 B1 证券清单 {path}：先运行 scripts/forward_universe.py 生成。"
            "没有清单不得生成正式前瞻信号。"
        )
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        securities = manifest["securities"]
        codes = [item["code"] for item in securities if item.get("forward_eligible")]
    except Exception as error:  # noqa: BLE001
        raise SystemExit(f"B1 证券清单无法解析：{error}") from error
    if not codes:
        raise SystemExit("B1 证券清单里没有可前瞻证券")
    expected = manifest.get("counts", {}).get("forward_eligible")
    if expected is not None and int(expected) != len(codes):
        raise SystemExit("B1 证券清单的 counts.forward_eligible 与实际条目数不一致")
    return {
        "version": manifest.get("version", "unknown"),
        "sha256": manifest.get("universe_sha256", ""),
        "eligibility_sha256": manifest.get("eligibility_sha256", ""),
        "data_cutoff": manifest.get("data_cutoff"),
        "codes": codes,
        "eligible": len(codes),
        "excluded_unverified": len(securities) - len(codes),
    }


def _paths(out_dir: Path, day: date, mode: str) -> tuple[Path, Path]:
    stem = (
        f"signals_{day.isoformat()}"
        if mode == MODE_FORWARD
        else f"replay_signals_{day.isoformat()}"
    )
    return out_dir / f"{stem}.jsonl", out_dir / f"{stem}.meta.json"


def _write_tmp(final: Path, payload: bytes) -> Path:
    """把内容完整写入同目录临时文件并 fsync；崩溃只会留下 .tmp-*。

    写失败（磁盘满、权限、被中断）时自己清掉半截临时文件，不让它留在目录里。
    """
    tmp = final.with_name(f".{final.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _publish(tmp: Path, final: Path) -> bool:
    """原子发布：已存在则返回 False 且**不覆盖**。

    用 `os.link` 而不是 `os.replace`：`replace` 会无条件覆盖，正好是并发/重复运行
    时最危险的行为。`link` 在目标存在时抛 `FileExistsError`，天然是「只有第一个
    成功」的独占创建。
    """
    try:
        os.link(tmp, final)
        return True
    except FileExistsError:
        return False
    except OSError:
        # 少数文件系统不支持硬链接：退化为独占创建 + 复制
        try:
            fd = os.open(
                final,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
            )
        except FileExistsError:
            return False
        with os.fdopen(fd, "wb") as handle:
            try:
                handle.write(tmp.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            except BaseException:
                # 复制中断时别留下半截目标文件（它会被误认为已发布）
                handle.close()
                final.unlink(missing_ok=True)
                raise
        return True


# ---------------------------------------------------------------------------
# 快照状态机：一次冻结要落两个文件，中间崩溃会留下「半套」
# ---------------------------------------------------------------------------

STATE_NONE = "NONE"
STATE_COMPLETE = "COMPLETE"
STATE_INCOMPLETE = "INCOMPLETE_PUBLISH"
STATE_CORRUPTED = "CORRUPTED_SNAPSHOT"


class SnapshotError(Exception):
    """带机器可读代码的快照错误（INCOMPLETE_PUBLISH / CORRUPTED_SNAPSHOT）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SnapshotState:
    state: str
    detail: str
    meta: dict | None = None


def _inspect_guide(out_dir: Path, day: date, mode: str) -> str:
    jsonl, meta_path = _paths(out_dir, day, mode)
    return (
        "人工检查与安全处置（本工具不会自动删除、也不会自动重建任何正式文件）：\n"
        f"  1) 目录：{out_dir}\n"
        f"  2) 只有 JSONL、没有 meta：发布在 meta 之前中断，这份 JSONL 从未成为有效快照。\n"
        f"     确认该日没有其他有效记录后，人工删除：Remove-Item \"{jsonl}\"\n"
        f"  3) 只有 meta、没有 JSONL：信号内容已丢失，无法恢复；确认后人工删除：\n"
        f"     Remove-Item \"{meta_path}\"\n"
        "  4) 处理完再重新运行 snapshot。\n"
        "  ⚠️ 不要手工补写 meta —— 那等于把事后重建的记录伪装成当日前瞻信号。"
    )


def inspect_snapshot(out_dir: Path, day: date, mode: str) -> SnapshotState:
    """判定某日某模式的快照状态；不做任何写操作。"""
    jsonl, meta_path = _paths(out_dir, day, mode)
    has_jsonl = jsonl.is_file()
    has_meta = meta_path.is_file()
    if not has_jsonl and not has_meta:
        return SnapshotState(STATE_NONE, "没有同名快照")
    if has_jsonl != has_meta:
        return SnapshotState(
            STATE_INCOMPLETE,
            "JSONL 已发布但 meta 缺失" if has_jsonl else "meta 已存在但 JSONL 缺失",
        )
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as error:  # noqa: BLE001
        return SnapshotState(STATE_CORRUPTED, f"meta 无法解析：{error}")
    if not isinstance(meta, dict):
        return SnapshotState(STATE_CORRUPTED, "meta 不是 JSON 对象")
    if meta.get("mode") != MODE_LABELS[mode]:
        return SnapshotState(
            STATE_CORRUPTED,
            f"meta 模式标记为 {meta.get('mode')}，期望 {MODE_LABELS[mode]}",
            meta,
        )
    digest = hashlib.sha256(jsonl.read_bytes()).hexdigest()
    if meta.get("content_sha256") != digest:
        return SnapshotState(STATE_CORRUPTED, "JSONL 内容哈希与 meta 不一致", meta)
    return SnapshotState(STATE_COMPLETE, "哈希自洽", meta)


def load_snapshot_rows(
    out_dir: Path, day: date, mode: str, meta: dict
) -> list[dict]:
    """读取并做行级一致性校验；任何不一致都按 CORRUPTED_SNAPSHOT 处理。"""
    jsonl, _meta_path = _paths(out_dir, day, mode)
    try:
        lines = jsonl.read_bytes().decode("utf-8").splitlines()
        rows = [json.loads(line) for line in lines if line.strip()]
    except Exception as error:  # noqa: BLE001
        raise SnapshotError(STATE_CORRUPTED, f"JSONL 无法解析：{error}") from error
    if not rows:
        raise SnapshotError(STATE_CORRUPTED, "JSONL 没有任何记录")
    if meta.get("row_count") != len(rows):
        raise SnapshotError(
            STATE_CORRUPTED,
            f"meta 记录 {meta.get('row_count')} 行，实际 {len(rows)} 行",
        )
    for row in rows:
        if row.get("signal_date") != day.isoformat():
            raise SnapshotError(
                STATE_CORRUPTED,
                f"记录里的 signal_date={row.get('signal_date')}，与文件名 {day} 不符",
            )
        if row.get("mode") != MODE_LABELS[mode]:
            raise SnapshotError(
                STATE_CORRUPTED,
                f"记录里的 mode={row.get('mode')}，与快照模式 {MODE_LABELS[mode]} 不符",
            )
        if row.get("strategy_version") != meta.get("strategy_version"):
            raise SnapshotError(
                STATE_CORRUPTED,
                "记录里的 strategy_version 与 meta 不一致",
            )
    return rows


def _publish_pair(jsonl_payload: bytes, meta_payload: bytes, jsonl: Path, meta: Path) -> bool:
    """先发布 JSONL，再发布 meta（meta 是「有效快照」的提交标记）。

    返回 False 表示已存在同名快照，调用方必须放弃且不得修改旧文件。
    若 meta 已存在但 JSONL 不存在（历史上写入中断），本次会撤掉刚发布的 JSONL，
    使状态回到「没有有效快照」，而不是留下半套。
    """
    jsonl_tmp: Path | None = None
    meta_tmp: Path | None = None
    published_jsonl = False
    try:
        jsonl_tmp = _write_tmp(jsonl, jsonl_payload)
        meta_tmp = _write_tmp(meta, meta_payload)
        if not _publish(jsonl_tmp, jsonl):
            return False
        published_jsonl = True
        if not _publish(meta_tmp, meta):
            logger.error("meta 已存在但 JSONL 缺失：已回滚本次 JSONL，请人工检查 %s", meta)
            jsonl.unlink(missing_ok=True)
            published_jsonl = False
            return False
        published_jsonl = False  # 成功：保留 JSONL
        return True
    except BaseException:
        if published_jsonl:
            jsonl.unlink(missing_ok=True)
        raise
    finally:
        if jsonl_tmp is not None:
            jsonl_tmp.unlink(missing_ok=True)
        if meta_tmp is not None:
            meta_tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 候选生成（与历史回测的 C0 完全同一条代码路径）
# ---------------------------------------------------------------------------


def build_candidates(
    matrix, day: date, top_n: int = TOP_N, universe: list[str] | None = None
) -> dict:
    """生成 D 日的 C0 候选。

    **直接调用 `backtest_reversal._day_candidates` + `_rank_top`** —— 与历史回测
    的 C0 是同一段代码，不是"两份长得像的实现"。任何一方改公式，另一方立刻跟着变，
    不会出现"前瞻协议悄悄漂移、历史结论不再适用"。

    `universe` 非空时只在清单内取候选（B1 前瞻池）；为 None 时与历史回测完全一致。
    """
    if day not in matrix.date_pos:
        raise SystemExit(f"{day} 不在行情矩阵里（非交易日或无数据）")
    pos = matrix.date_pos[day]
    cols = _code_columns(matrix)
    j_of = {code: j for j, code in enumerate(matrix.codes)}

    pool = _day_candidates(matrix, pos, cols, j_of, universe)
    if pool is None:
        raise SystemExit(f"{day} 当日有效候选不足 {MIN_POOL} 只，拒绝生成")
    series, liquidity, scores, percent = pool

    scored = [(code, item.score) for code, item in scores.items()]
    codes = _rank_top(scored, top_n)
    total_of = dict(scored)

    return {
        "day": day,
        "pos": pos,
        "pool": sorted(series),
        "scores": scores,
        "percentiles": percent,
        "liquidity": liquidity,
        "ranked": [(code, total_of[code]) for code in codes],
    }


def _factor_missing_ratio(candidates: dict) -> float:
    scores = candidates["scores"]
    if not scores:
        return 1.0
    missing = sum(
        1
        for item in scores.values()
        if item.rev20 is None or item.illiq20 is None or item.amp20 is None
    )
    return missing / len(scores)


# ---------------------------------------------------------------------------
# forward 闸门
# ---------------------------------------------------------------------------


def _board_of(code: str) -> str:
    """按代码段判市场。北交所包含老段 43/83/87/88 与 2024 年起的 920 段。"""
    if code.startswith("60"):
        return "沪主板"
    if code.startswith("68"):
        return "科创板"
    if code.startswith("00"):
        return "深主板"
    if code.startswith("30"):
        return "创业板"
    if code[:2] in ("43", "83", "87", "88", "92"):
        return "北交所"
    return "其他"


def _board_counts(session: Session, day: date) -> dict[str, int]:
    counts: dict[str, int] = {}
    for code in session.scalars(
        select(StockDaily.code).where(StockDaily.trade_date == day)
    ):
        board = _board_of(code)
        counts[board] = counts.get(board, 0) + 1
    return counts


def _completeness(session: Session, day: date) -> dict:
    rows = int(
        session.scalar(
            select(func.count()).select_from(StockDaily).where(StockDaily.trade_date == day)
        )
        or 0
    )
    null_ohlc = int(
        session.scalar(
            select(func.count())
            .select_from(StockDaily)
            .where(
                StockDaily.trade_date == day,
                or_(
                    StockDaily.open.is_(None),
                    StockDaily.high.is_(None),
                    StockDaily.low.is_(None),
                    StockDaily.close.is_(None),
                    StockDaily.pct_chg.is_(None),
                ),
            )
        )
        or 0
    )
    null_amount = int(
        session.scalar(
            select(func.count())
            .select_from(StockDaily)
            .where(StockDaily.trade_date == day, StockDaily.amount.is_(None))
        )
        or 0
    )
    previous = list(
        session.scalars(
            select(StockDaily.trade_date)
            .where(StockDaily.trade_date < day)
            .distinct()
            .order_by(StockDaily.trade_date.desc())
            .limit(5)
        )
    )
    counts = sorted(
        int(
            session.scalar(
                select(func.count()).select_from(StockDaily).where(StockDaily.trade_date == item)
            )
            or 0
        )
        for item in previous
    )
    median_rows = counts[len(counts) // 2] if counts else 0

    # 分市场覆盖：只看总行数无法发现「北交所整段消失、同时沪深多出几只」这种等量替换
    board_today = _board_counts(session, day)
    history_boards = [_board_counts(session, item) for item in previous]
    boards: dict[str, dict] = {}
    for board in BOARD_NAMES:
        today_rows = board_today.get(board, 0)
        series = sorted(item.get(board, 0) for item in history_boards)
        board_median = series[len(series) // 2] if series else 0
        boards[board] = {
            "rows": today_rows,
            "median_5d": board_median,
            "ratio": (today_rows / board_median) if board_median else 0.0,
        }
    return {
        "trade_date": day.isoformat(),
        "rows": rows,
        "previous_days": [item.isoformat() for item in previous],
        "median_rows_5d": median_rows,
        "boards": boards,
        "coverage_ratio": (rows / median_rows) if median_rows else 0.0,
        "null_ohlc_rows": null_ohlc,
        "null_amount_rows": null_amount,
        "null_ohlc_ratio": (null_ohlc / rows) if rows else 1.0,
        "null_amount_ratio": (null_amount / rows) if rows else 1.0,
    }


def forward_problems(session: Session, day: date, now: datetime) -> list[str]:
    """返回 forward 模式不满足的条件（空列表 = 可以冻结）。"""
    problems: list[str] = []
    if now.tzinfo is None:
        raise ValueError("now 必须带时区")
    now = now.astimezone(MARKET_TZ)

    try:
        has_calendar = (
            session.scalar(select(func.count()).select_from(TradeCalendar)) or 0
        ) > 0
    except Exception:  # noqa: BLE001
        session.rollback()
        has_calendar = False
    if not has_calendar:
        # 没有交易日历就无法证明「D 是交易日」「D 的次日开盘在什么时候」，
        # 这两条是前瞻时间约束的地基 —— 缺了就拒绝，不拿自然日凑。
        problems.append(
            "该库没有交易日历（trade_calendar 表不存在或为空）："
            "无法证明 D 是交易日与次日开盘时点"
        )
        return problems

    if not trading_days.is_trade_day(day, session):
        problems.append(f"{day} 不在交易日历里，不是有效交易日")

    ready_at = datetime.combine(day, DATA_READY_AT, tzinfo=MARKET_TZ)
    if now < ready_at:
        problems.append(f"当前 {now:%Y-%m-%d %H:%M} 早于 D 日 {DATA_READY_AT:%H:%M}（收盘+落库等待）")

    next_day = trading_days.next_trade_day(day, session)
    if next_day is None:
        problems.append("交易日历里找不到 D 的下一交易日：无法确认「下一交易日开盘前」")
    else:
        next_open = datetime.combine(next_day, NEXT_OPEN_AT, tzinfo=MARKET_TZ)
        if now >= next_open:
            problems.append(
                f"当前 {now:%Y-%m-%d %H:%M} 已过下一交易日 {next_day} {NEXT_OPEN_AT:%H:%M}，"
                "此时冻结不是「开盘前」"
            )

    latest = session.scalar(select(func.max(StockDaily.trade_date)))
    if latest is None:
        problems.append("研究库是空的")
        return problems
    if latest != day:
        problems.append(
            f"库里最新交易日是 {latest}，不是 {day}：没有当日行情，拒绝用旧数据冒充"
        )

    info = _completeness(session, day)
    if info["rows"] == 0:
        problems.append(f"{day} 没有任何行情记录")
    else:
        if info["coverage_ratio"] < MIN_COVERAGE_RATIO:
            problems.append(
                f"{day} 覆盖 {info['rows']} 行，仅为近 5 日中位数 "
                f"{info['median_rows_5d']} 的 {info['coverage_ratio']:.1%}（< {MIN_COVERAGE_RATIO:.0%}）"
            )
        if info["null_ohlc_ratio"] > MAX_NULL_RATIO:
            problems.append(
                f"{day} OHLC/pct_chg 缺失 {info['null_ohlc_rows']} 行"
                f"（{info['null_ohlc_ratio']:.2%} > {MAX_NULL_RATIO:.1%}）"
            )
        if info["null_amount_ratio"] > MAX_NULL_RATIO:
            problems.append(
                f"{day} 成交额缺失 {info['null_amount_rows']} 行"
                f"（{info['null_amount_ratio']:.2%} > {MAX_NULL_RATIO:.1%}）"
            )
        # 分市场覆盖：总行数接近不代表证券范围完整（北交所整段消失 + 沪深多几只，
        # 总数可以几乎不变）。任何近期稳定的市场当天掉超过 5% 都拒绝。
        for board, item in info["boards"].items():
            if item["median_5d"] < MIN_BOARD_ROWS:
                continue
            if item["ratio"] < MIN_COVERAGE_RATIO:
                problems.append(
                    f"{board} 覆盖 {item['rows']} 行，近 5 日中位数 {item['median_5d']}，"
                    f"仅 {item['ratio']:.1%}（< {MIN_COVERAGE_RATIO:.0%}）：疑似该市场被静默丢弃"
                )
    return problems


def _parse_now(text: str) -> datetime:
    if not text:
        return datetime.now(MARKET_TZ)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MARKET_TZ)
    return parsed.astimezone(MARKET_TZ)


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def cmd_snapshot(args) -> int:
    db_path = Path(args.db)
    out_dir = Path(args.out)
    mode = args.mode
    if not db_path.is_file():
        raise SystemExit(f"没有 {db_path}")
    out_dir.mkdir(parents=True, exist_ok=True)

    engine = _readonly_engine(db_path)
    now = _parse_now(getattr(args, "now", ""))
    try:
        with Session(engine) as session:
            latest = session.scalar(select(func.max(StockDaily.trade_date)))
            if latest is None:
                raise SystemExit("研究库是空的")

            if mode == MODE_REPLAY:
                day = date.fromisoformat(args.date) if args.date else latest
                if day > latest:
                    raise SystemExit(f"{day} 超出库内最新交易日 {latest}，无法重放")
                completeness = None
            else:
                day = date.fromisoformat(args.date) if args.date else now.date()
                completeness = None

            # 先判状态再做重活：已有完整/半套快照时不该白算几分钟
            state = inspect_snapshot(out_dir, day, mode)
            if state.state == STATE_COMPLETE:
                logger.warning("%s 的 %s 快照已存在（COMPLETE），拒绝覆盖", day, mode)
                print(
                    f"拒绝覆盖：{day} 的 {mode} 快照已完整存在。"
                    "如需重做，请先人工移走旧文件（工具不会自动删除正式记录）。"
                )
                return 2
            if state.state == STATE_INCOMPLETE:
                print(f"{STATE_INCOMPLETE}：{state.detail}", file=sys.stderr)
                print(_inspect_guide(out_dir, day, mode), file=sys.stderr)
                return 4
            if state.state == STATE_CORRUPTED:
                print(f"{STATE_CORRUPTED}：{state.detail}", file=sys.stderr)
                print(_inspect_guide(out_dir, day, mode), file=sys.stderr)
                return 5

            universe_block = None
            universe_codes = None
            if mode == MODE_FORWARD:
                manifest = load_universe(Path(args.universe))
                universe_codes = manifest["codes"]
                universe_block = {
                    "version": manifest["version"],
                    "sha256": manifest["sha256"],
                    "eligibility_sha256": manifest["eligibility_sha256"],
                    "data_cutoff": manifest["data_cutoff"],
                    "eligible": manifest["eligible"],
                    "excluded_unverified": manifest["excluded_unverified"],
                    "applied": True,
                }
                problems = forward_problems(session, day, now)
                if problems:
                    print(f"拒绝正式前瞻冻结 {day}（forward）：", file=sys.stderr)
                    for item in problems:
                        print(f"  - {item}", file=sys.stderr)
                    print(
                        "\n没有及时、合规的当日数据源时，保持 FORWARD_TRACKING_NOT_STARTED；"
                        "需要验证代码或策略一致性请用 --mode replay（输出标记 REPLAY_ONLY）。",
                        file=sys.stderr,
                    )
                    return 3
                completeness = _completeness(session, day)

            matrix = load_matrix(session, day - timedelta(days=90), day)
            candidates = build_candidates(matrix, day, args.top, universe_codes)
            missing = _factor_missing_ratio(candidates)
            if mode == MODE_FORWARD and missing > MAX_FACTOR_MISSING_RATIO:
                print(
                    f"拒绝正式前瞻冻结 {day}：三因子缺失率 {missing:.2%} > "
                    f"{MAX_FACTOR_MISSING_RATIO:.0%}，数据不可用于正式记录",
                    file=sys.stderr,
                )
                return 3

            revision = _data_revision(session, db_path)
            data_source = detect_data_source(session, db_path, day, revision)
            if mode == MODE_FORWARD:
                completeness["factor_missing_ratio"] = round(missing, 6)
                completeness["verified_at"] = now.isoformat(timespec="seconds")
    finally:
        engine.dispose()

    created_at = now.isoformat(timespec="seconds")
    rows = []
    for rank, (code, total) in enumerate(candidates["ranked"], start=1):
        item = candidates["scores"][code]
        rows.append(
            {
                "mode": MODE_LABELS[mode],
                "signal_date": day.isoformat(),
                "created_at": created_at,
                "strategy_version": STRATEGY_VERSION,
                "git_commit": _git_commit(),
                "source_data_date": latest.isoformat(),
                "source_data_revision": revision,
                "universe_definition": PROTOCOL["universe_definition"],
                "universe_count": len(candidates["pool"]),
                "stock_code": code,
                "rank": rank,
                "score_total": total,
                "rev20": item.rev20,
                "illiq20": item.illiq20,
                "amp20": item.amp20,
                "percentiles": candidates["percentiles"][code],
                "avg_amount_20": round(candidates["liquidity"].get(code, 0.0), 2),
                "eligibility_reason": "20日窗口>=18行 + D日有行情",
            }
        )

    payload = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    meta = {
        "mode": MODE_LABELS[mode],
        "counts_for_forward_performance": mode == MODE_FORWARD,
        "signal_date": day.isoformat(),
        "created_at": created_at,
        "created_at_tz": "Asia/Shanghai",
        "strategy_version": STRATEGY_VERSION,
        "git_commit": rows[0]["git_commit"],
        "source_data_date": latest.isoformat(),
        "source_data_revision": revision,
        "data_source": data_source,
        "protocol": PROTOCOL,
        "row_count": len(rows),
        "universe_count": len(candidates["pool"]),
        "completeness": completeness,
        "universe": universe_block,
        "content_sha256": digest,
        "integrity_note": (
            "content_sha256 仅校验 JSONL 与 meta 是否自洽，可发现单文件改动或写入中断；"
            "不能证明创建时间，也不能防止两者被同时替换。无独立时间戳。"
        ),
    }
    if mode == MODE_REPLAY:
        meta["replay_note"] = "历史重放：仅用于代码测试与策略一致性验证，不计入前瞻绩效"

    jsonl_path, meta_path = _paths(out_dir, day, mode)
    published = _publish_pair(
        payload.encode("utf-8"),
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
        jsonl_path,
        meta_path,
    )
    if not published:
        logger.warning("并发或重复运行：%s 的 %s 快照已存在，本次未写入", day, mode)
        state = inspect_snapshot(out_dir, day, mode)
        if state.state == STATE_INCOMPLETE:
            print(f"{STATE_INCOMPLETE}：{state.detail}", file=sys.stderr)
            print(_inspect_guide(out_dir, day, mode), file=sys.stderr)
            return 4
        if state.state == STATE_CORRUPTED:
            print(f"{STATE_CORRUPTED}：{state.detail}", file=sys.stderr)
            print(_inspect_guide(out_dir, day, mode), file=sys.stderr)
            return 5
        return 2

    banner = "REPLAY_ONLY（不计入前瞻绩效）" if mode == MODE_REPLAY else "FORWARD_FROZEN"
    print(f"[{banner}] {day}：{len(rows)} 只候选，池 {len(candidates['pool'])} 只")
    print(f"  生成时间 {created_at}（Asia/Shanghai），sha256={digest[:16]}…")
    print(f"  {jsonl_path}")
    print(f"  {meta_path}")
    return 0


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------


def evaluation_summary(samples, *, horizon: int = HORIZON) -> dict:
    """把样本拆成「执行统计」与「理论观察统计」，两者绝不混用同一均值。"""
    rows = [sample for sample in samples if sample.horizon == horizon]
    counts: dict[str, int] = {}
    for sample in rows:
        counts[sample.status] = counts.get(sample.status, 0) + 1
    ok = [s for s in rows if s.status == "ok" and s.gross is not None]
    observation = [
        s for s in rows if s.status in ("ok", "exit_limit_down") and s.gross is not None
    ]
    limited = [s for s in observation if s.status == "exit_limit_down"]
    pending = [s for s in rows if s.status == "pending"]
    total = len(rows)
    matured = total - len(pending)

    def mean(values):
        values = [value for value in values if value is not None]
        return (sum(values) / len(values)) if values else None

    return {
        "total": total,
        "counts": counts,
        "ok_count": len(ok),
        "observation_count": len(observation),
        "limited_count": len(limited),
        "pending_count": len(pending),
        "matured_count": matured,
        "pending_ratio": (len(pending) / total) if total else 0.0,
        "matured_ratio": (matured / total) if total else 0.0,
        "ok_gross": mean([s.gross for s in ok]),
        "ok_net": {cost: mean([_net(s.gross, cost) for s in ok]) for cost in COSTS},
        "ok_benchmark": mean([s.benchmark for s in ok]),
        "ok_excess": mean([s.excess for s in ok]),
        "observation_gross": mean([s.gross for s in observation]),
        "limited_gross": mean([s.gross for s in limited]),
    }


def _fmt(value, suffix: str = "%") -> str:
    return "N/A" if value is None else f"{value:+.2f}{suffix}"


def _print_evaluation(day: date, mode: str, summary: dict) -> None:
    """正式/重放评价展示：执行收益与跌停理论观察分段输出。"""
    flag = "" if mode == MODE_FORWARD else "  [REPLAY_ONLY · 不计入前瞻绩效]"
    print(f"快照 {day}（{summary['total']} 笔信号，哈希校验通过）{flag}")
    print(f"评价器：{EVALUATOR_VERSION}（git {_git_commit()[:8]}）")
    order = ("pending", "unbuyable", "suspended", "no_exit", "ok",
             "exit_limit_down", "no_quote", "price_invalid", "new_listing")
    counts = summary["counts"]
    print("状态：" + "，".join(f"{key}={counts[key]}" for key in order if counts.get(key)))
    print(
        f"成熟度：成熟 {summary['matured_count']}/{summary['total']}"
        f"（{summary['matured_ratio']:.1%}），"
        f"未成熟 pending {summary['pending_count']}/{summary['total']}"
        f"（{summary['pending_ratio']:.1%}）"
    )

    if summary["total"] == 0:
        print("没有可评价的样本。")
        return
    if summary["pending_count"] == summary["total"]:
        print(
            "全部 pending：不输出持有期收益，也不计算没有分母的收益率；"
            "pending 不计入任何收益。"
        )
        return

    if summary["ok_count"]:
        print(f"\n正常完成（ok，可执行）：{summary['ok_count']} 笔")
        print(f"  毛均值 {_fmt(summary['ok_gross'])}")
        for cost in COSTS:
            print(f"  {int(cost):>3}bps 净均值 {_fmt(summary['ok_net'][cost])}")
        if summary["ok_benchmark"] is not None:
            print(f"  基准毛均值 {_fmt(summary['ok_benchmark'])}")
        if summary["ok_excess"] is not None:
            print(f"  毛超额 {_fmt(summary['ok_excess'])}")
    else:
        print("\n正常完成（ok，可执行）：0 笔；毛/净收益 N/A（没有可执行样本）")

    if summary["observation_count"]:
        print(
            f"\n已入场且有退出价（理论观察，含 exit_limit_down）："
            f"{summary['observation_count']} 笔，理论毛均值 "
            f"{_fmt(summary['observation_gross'])}"
        )
        if summary["limited_count"]:
            print(
                f"  其中 exit_limit_down {summary['limited_count']} 笔，"
                f"理论毛均值 {_fmt(summary['limited_gross'])}"
                "（跌停退出可能卖不出，不代表真实成交）"
            )
    if summary["pending_count"]:
        print(f"\n未成熟（pending）{summary['pending_count']} 笔：不计入任何收益")

    print()
    print("口径：信号层（SIGNAL_LEVEL_ONLY）；")
    print("      正常完成均值只含 ok；exit_limit_down 只作理论观察，不并入执行收益；")
    print("      未完成退出不计收益；基准为同日等权毛收益，未扣费。")


def cmd_evaluate(args) -> int:
    db_path = Path(args.db)
    out_dir = Path(args.out)
    day = date.fromisoformat(args.date)
    mode = args.mode
    jsonl_path, meta_path = _paths(out_dir, day, mode)

    state = inspect_snapshot(out_dir, day, mode)
    if state.state == STATE_NONE:
        raise SystemExit(f"没有 {day} 的 {mode} 快照：{jsonl_path}")
    if state.state == STATE_INCOMPLETE:
        raise SystemExit(
            f"{STATE_INCOMPLETE}: {state.detail}。这不是有效快照，不进入前瞻绩效。\n"
            + _inspect_guide(out_dir, day, mode)
        )
    if state.state == STATE_CORRUPTED:
        raise SystemExit(
            f"{STATE_CORRUPTED}: {state.detail}。拒绝评价。\n"
            + _inspect_guide(out_dir, day, mode)
        )

    meta = state.meta or {}
    if meta.get("strategy_version") != STRATEGY_VERSION:
        raise SystemExit(
            f"快照策略版本 {meta.get('strategy_version')} ≠ 当前 {STRATEGY_VERSION}："
            "协议已变更，不能用旧快照冒充新协议"
        )
    if mode == MODE_FORWARD and not meta.get("completeness"):
        raise SystemExit("正式快照缺少数据完整性记录，拒绝评价")

    try:
        rows = load_snapshot_rows(out_dir, day, mode, meta)
    except SnapshotError as error:
        raise SystemExit(f"{error}\n{_inspect_guide(out_dir, day, mode)}") from error

    signals = [
        Signal(code=row["stock_code"], signal_date=day, meta={"rank": row["rank"]})
        for row in rows
    ]

    engine = _readonly_engine(db_path)
    try:
        with Session(engine) as session:
            current_revision = _data_revision(session, db_path)
            mismatch = revision_mismatch(meta, current_revision, day)
            if mismatch:
                raise SystemExit(mismatch)
            if mode == MODE_FORWARD:
                for problem in _forward_audit_problems(session, day, meta):
                    raise SystemExit(problem)
            latest = session.scalar(select(func.max(StockDaily.trade_date)))
            matrix = load_matrix(session, day, min(latest, day + timedelta(days=60)))
            benchmark = Benchmark(session, matrix, "market")
            result = run_signals(
                session,
                signals,
                strategy=f"C0 {mode} {day}",
                params={"top": TOP_N, "version": STRATEGY_VERSION},
                horizons=(HORIZON,),
                entry_mode="next_open",
                cost_bps=0.0,
                matrix=matrix,
                benchmark=benchmark,
            )
    finally:
        engine.dispose()

    summary = evaluation_summary(result.samples)
    if summary["total"] == 0:
        raise SystemExit("没有可评价的样本")
    _print_evaluation(day, mode, summary)
    return 0


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def cmd_list(args) -> int:
    out_dir = Path(args.out)
    if not out_dir.is_dir():
        print("还没有任何快照")
        return 0

    # 同时扫 .jsonl 与 .meta.json：只有一半的孤儿文件也必须列出来，
    # 否则「某天其实没冻结成功」会被悄悄忽略。
    entries: dict[tuple[date, str], None] = {}
    for path in out_dir.iterdir():
        if path.name.startswith("."):
            continue
        if path.suffix not in (".jsonl", ".json"):
            continue
        stem = path.name[: -len(".meta.json")] if path.name.endswith(".meta.json") else path.stem
        mode = MODE_REPLAY if stem.startswith("replay_signals_") else MODE_FORWARD
        raw = stem[len("replay_signals_") :] if mode == MODE_REPLAY else stem[len("signals_") :]
        try:
            entries[(date.fromisoformat(raw), mode)] = None
        except ValueError:
            continue

    if not entries:
        print("还没有任何快照")
        return 0
    for day, mode in sorted(entries):
        state = inspect_snapshot(out_dir, day, mode)
        meta = state.meta or {}
        print(
            f"{MODE_LABELS[mode]:<15}{day.isoformat():<12}{state.state:<18} "
            f"rows={meta.get('row_count', '-'):<4} pool={meta.get('universe_count', '-'):<5} "
            f"sha={str(meta.get('content_sha256', ''))[:12]}…"
        )
        if state.state != STATE_COMPLETE:
            print(f"  ↳ {state.detail}（不进入前瞻绩效，见 snapshot/evaluate 的处置提示）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C0 前瞻纸面跟踪（本地手动）")
    parser.add_argument("--db", default=str(HISTORY_DB))
    parser.add_argument("--out", default=str(OUT_DIR))
    parser.add_argument(
        "--universe",
        default=str(UNIVERSE_FILE),
        help="B1 证券清单 JSON（仅 forward 使用；缺失则拒绝正式冻结）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    snap = sub.add_parser("snapshot", help="生成候选快照")
    snap.add_argument(
        "--mode",
        required=True,
        choices=(MODE_FORWARD, MODE_REPLAY),
        help="forward=真实前瞻冻结（默认拒绝历史/过期数据）；replay=历史重放（标记 REPLAY_ONLY）",
    )
    snap.add_argument("--date", default="", help="D 日；forward 默认今天，replay 默认库内最新日")
    snap.add_argument("--top", type=int, default=TOP_N, help="每日取前 N 只")
    snap.add_argument("--now", default="", help="测试注入当前时间（ISO8601，默认取系统时间）")
    snap.set_defaults(func=cmd_snapshot)

    ev = sub.add_parser("evaluate", help="评价已冻结快照")
    ev.add_argument("--date", required=True)
    ev.add_argument(
        "--mode", default=MODE_FORWARD, choices=(MODE_FORWARD, MODE_REPLAY)
    )
    ev.set_defaults(func=cmd_evaluate)

    ls = sub.add_parser("list", help="列出已有快照")
    ls.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
