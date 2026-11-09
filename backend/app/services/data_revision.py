"""研究缓存的数据修订水位与安全状态（R5 加固）。

原则：**缓存只在校验通过时启用，校验不过就关闭**（fail closed）。

- `data_revision(domain, revision)`：每个数据域一行，由触发器在同一事务内推进；
- `data_revision_meta(key='db_uuid')`：库身份；
- 读取前检查：修订表存在、身份存在、**所有依赖表对应的触发器都在**、修订可读、
  数据库文件身份可确定；任一不满足 → 该请求不写缓存、不读缓存，直接查库；
- 不再用 `MAX(trade_date)`、文件 mtime 或 `PRAGMA data_version` 作为“数据未变”的
  正确性依据；它们只在诊断日志里出现；
- 数据库不可读时让异常向上抛，绝不用旧缓存冒充当前结果。

安装只允许在隔离库或经批准的环境执行：`scripts/install_data_revision.py --db <path>`。
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: 数据域 → 依赖的物理表。研究响应实际读到的表都必须在这里（见 R5-02 依赖矩阵）。
DOMAINS: dict[str, tuple[str, ...]] = {
    # 研究指标直接读取
    "stock_daily": ("stock_daily",),
    "stock_basic": ("stock_basic",),
    "stock_universe": ("stock_universe",),
    "trade_calendar": ("trade_calendar",),
    "rps": ("rps_daily",),
    "sector_daily": ("sector_daily",),
    "sector_member": ("sector_member",),
    # 历史快照完整性状态（研究覆盖说明 / 后续 B 类指标）
    "backfill_manifest": ("sector_member_backfill",),
}

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS data_revision (
    domain TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0
)
"""

_META_SQL = """
CREATE TABLE IF NOT EXISTS data_revision_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_SUFFIXES = ("ai", "au", "ad")


def trigger_name(table: str, suffix: str) -> str:
    return f"trg_{table}_{suffix}"


def _trigger_sql(table: str, domain: str) -> list[str]:
    statements = []
    for action, suffix in (("INSERT", "ai"), ("UPDATE", "au"), ("DELETE", "ad")):
        statements.append(
            f"""
CREATE TRIGGER IF NOT EXISTS {trigger_name(table, suffix)}
AFTER {action} ON {table}
BEGIN
    INSERT INTO data_revision(domain, revision) VALUES ('{domain}', 1)
    ON CONFLICT(domain) DO UPDATE SET revision = revision + 1;
END
"""
        )
    return statements


@dataclass(frozen=True)
class CacheState:
    """缓存的可用状态。`state` 只取 ready / not_installed / unverifiable。"""

    state: str
    reason: str = ""
    identity: tuple | None = None
    version: tuple | None = None

    @property
    def ready(self) -> bool:
        return self.state == "ready"


_log_state: tuple[str, float] = ("", 0.0)


def _log_unavailable(state: str, reason: str) -> None:
    """同一原因最多每 60 秒记一次，避免每个请求都刷日志。"""
    global _log_state
    now = time.monotonic()
    if _log_state[0] == reason and now - _log_state[1] < 60:
        return
    _log_state = (reason, now)
    logger.warning("研究结果缓存已关闭（%s）：%s", state, reason)


def _file_identity(session: Session) -> tuple | None:
    """库身份：绝对路径 + 设备号 + inode。路径相同但文件被替换/inode 变化时身份不同。"""
    database = session.get_bind().url.database
    if not database:
        return None
    path = Path(str(database))
    try:
        info = path.stat()
    except OSError:
        return None
    return (str(path.resolve()), info.st_dev, info.st_ino)


def status(session: Session) -> CacheState:
    """检查缓存是否可安全启用。任何异常都返回不可用（而不是抛给调用方）。"""
    try:
        connection = session.connection()
        tables = {
            row[0]
            for row in connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        if "data_revision" not in tables:
            return CacheState("not_installed", "缺少 data_revision 表")
        if "data_revision_meta" not in tables:
            return CacheState("not_installed", "缺少 data_revision_meta 表")
        triggers = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='trigger'")
            )
        }
        required = [
            trigger_name(table, suffix)
            for tables_for_domain in DOMAINS.values()
            for table in tables_for_domain
            if table in tables
            for suffix in _SUFFIXES
        ]
        missing = sorted(name for name in required if name not in triggers)
        if missing:
            return CacheState("not_installed", f"缺少触发器：{'、'.join(missing[:6])}")
        meta = connection.execute(
            text("SELECT value FROM data_revision_meta WHERE key = 'db_uuid'")
        ).first()
        if meta is None or not meta[0]:
            return CacheState("not_installed", "缺少 db_uuid")
        rows = connection.execute(
            text("SELECT domain, revision FROM data_revision ORDER BY domain")
        ).all()
        identity = _file_identity(session)
        if identity is None:
            return CacheState("unverifiable", "无法确定数据库文件身份")
        return CacheState(
            "ready",
            "",
            identity=("db", identity, meta[0]),
            version=("revision", tuple((row[0], row[1]) for row in rows)),
        )
    except Exception as exc:  # noqa: BLE001 - 任何读取失败都必须降级为“不可用”
        return CacheState("unverifiable", f"{type(exc).__name__}: {exc}")


def log_unavailable(state: CacheState) -> None:
    _log_unavailable(state.state, state.reason)


def cache_status(session: Session) -> dict:
    """给 `/api/research/meta` 的诊断信息（不含路径等敏感内容）。"""
    state = status(session)
    if not state.ready:
        log_unavailable(state)
    return {
        "state": state.state,
        "reason": state.reason,
        "identity": state.identity[2] if state.identity else None,
        "revisions": dict(state.version[1]) if state.version else None,
    }


def install(session: Session) -> str:
    """安装修订水位，返回库身份 `db_uuid`（幂等）。只用于隔离库/测试库。"""
    connection = session.connection()
    connection.execute(text(_TABLE_SQL))
    connection.execute(text(_META_SQL))
    row = connection.execute(
        text("SELECT value FROM data_revision_meta WHERE key = 'db_uuid'")
    ).first()
    if row is None:
        db_uuid = uuid.uuid4().hex
        connection.execute(
            text("INSERT INTO data_revision_meta(key, value) VALUES ('db_uuid', :v)"),
            {"v": db_uuid},
        )
    else:
        db_uuid = row[0]
    existing = {
        row[0]
        for row in connection.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        )
    }
    for domain, tables in DOMAINS.items():
        for table in tables:
            if table not in existing:
                continue
            for statement in _trigger_sql(table, domain):
                connection.execute(text(statement))
    session.commit()
    return db_uuid


def uninstall(session: Session) -> None:
    connection = session.connection()
    for tables in DOMAINS.values():
        for table in tables:
            for suffix in _SUFFIXES:
                connection.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name(table, suffix)}"))
    connection.execute(text("DROP TABLE IF EXISTS data_revision"))
    connection.execute(text("DROP TABLE IF EXISTS data_revision_meta"))
    session.commit()


def installed(session: Session) -> bool:
    return (
        session.connection()
        .execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='data_revision'")
        )
        .first()
        is not None
    )
