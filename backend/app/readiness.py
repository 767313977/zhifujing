"""灰度 readiness 契约（S6-B2）。

两条端点必须分开：

- `GET /api/health`：**只表示进程存活**（liveness）。配置错、离线锁定、数据库
  只读缺失都不影响它 —— 部署脚本靠它判断「端口起来了没有」。
- `GET /api/ready`：表示**所有灰度必要门禁**都通过。安全策略缺失时 liveness
  仍为真，但 readiness 必须是 false。

门禁状态是**三态字符串**，不是布尔：

- `PASS`：本进程内真实检查通过；
- `FAIL`：检查到明确不满足；
- `NOT_VERIFIED`：本环境无法验证（例如没有 OS 级网络隔离的访问权）；
- `NOT_APPLICABLE`：该环境不需要（本轮不使用）。

`ready=true` 的必要条件是**全部 required 门禁为 PASS**。本轮
`EGRESS_OS_POLICY_VERIFIED` 恒为 `NOT_VERIFIED`（无法接触 OS/网络层），
因此 readiness 必然 false，这是刻意保留的阻塞项，不允许用环境变量伪造。
"""

from __future__ import annotations

import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from typing import Any

from app import egress_policy, external_guard
from app.config import get_settings

PASS = "PASS"
FAIL = "FAIL"
NOT_VERIFIED = "NOT_VERIFIED"
NOT_APPLICABLE = "NOT_APPLICABLE"

CN_TZ = timezone(timedelta(hours=8))

#: 进程启动时由 lifespan 写入；未记录时按 FAIL 处理（不能默认放行）
_startup: dict[str, Any] = {
    "recorded": False,
    "schema_mode": None,
    "schema_result": None,
    "config_errors": [],
}


def record_startup(schema_result: dict | None = None) -> None:
    """在 `lifespan` 里调用，记录启动时真实生效的配置（**不写库**）。"""
    settings = get_settings()
    errors: list[str] = []
    if settings.schema_mode not in {"verify", "migrate"}:
        errors.append("SCHEMA_MODE_INVALID")
    env = (os.environ.get("FUPAN_ENV") or "development").strip().lower()
    if env == "production" and settings.enable_dev_cors:
        errors.append("DEV_CORS_ENABLED_IN_PRODUCTION")
    if env == "production" and settings.warmup_pattern_cache:
        # 预热本身不危险，但它会跑重查询；生产打开前应显式确认
        errors.append("WARMUP_CACHE_ENABLED_IN_PRODUCTION")
    _startup.update(
        {
            "recorded": True,
            "schema_mode": settings.schema_mode,
            "schema_result": schema_result,
            "config_errors": errors,
            "environment": env,
        }
    )


def startup_state() -> dict[str, Any]:
    return dict(_startup)


def probe_readonly_sqlite(db_path) -> tuple[str, str | None]:
    """在**同一文件**上开只读连接，尝试写操作，必须全部被拒绝。

    这验证的是 SQLite 的 `mode=ro` 语义（不是应用层自觉）。它不会修改主库：
    `mode=ro&immutable=1` 下任何写都会抛 `OperationalError`。
    """
    uri = f"file:{str(db_path).replace(chr(92), '/')}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        return FAIL, f"OPEN_FAILED:{type(exc).__name__}"
    try:
        attempts: list[str] = [
            # 始终可执行的写：DDL 与 pragma 写
            "CREATE TABLE __s6_probe (x INTEGER)",
            "PRAGMA user_version = 999",
        ]
        # 业务表存在时再补 DML 覆盖（表不存在时 DML 会因「无此表」失败，不能算证据）
        has_stock_daily = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_daily'"
            ).fetchone()
            is not None
        )
        if has_stock_daily:
            attempts.extend(
                [
                    "INSERT INTO stock_daily (trade_date) VALUES ('1990-01-01')",
                    "UPDATE stock_daily SET trade_date = trade_date WHERE 0",
                    "DELETE FROM stock_daily WHERE 0",
                ]
            )
        for statement in attempts:
            try:
                connection.execute(statement)
            except sqlite3.OperationalError:
                continue
            else:
                return FAIL, f"WRITE_SUCCEEDED:{statement.split()[0]}"
        return PASS, None
    finally:
        connection.close()


def _egress_app_gate() -> tuple[str, str | None]:
    snapshot = external_guard.stats()
    if not snapshot.get("installed") or not snapshot.get("dns_guarded"):
        return FAIL, "GUARD_NOT_INSTALLED"
    if snapshot.get("enabled"):
        policy = snapshot.get("policy") or {}
        if not policy.get("loaded"):
            return FAIL, policy.get("error") or "POLICY_NOT_LOADED"
        return PASS, None
    # 离线锁定：策略文件可以不存在（全部拒绝本来就是 fail closed）
    return PASS, None


def _file_is_readonly(path) -> bool | None:
    """文件权限是否只读。无法 stat 时返回 None（调用方按 FAIL 处理）。

    POSIX 看 mode 的写位；Windows 看只读属性（`os.chmod(S_IWRITE)` 位）。
    这是**文件系统层**的证据，与 SQLite 的 `mode=ro` 是两道独立防线。
    """
    try:
        st = path.stat()
    except OSError:
        return None
    if os.name == "nt":
        return not bool(st.st_mode & stat.S_IWRITE)
    return not bool(st.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _same_file(first, second) -> bool:
    """两个路径是否指向同一个文件（路径相同或同 inode）。"""
    try:
        if first.resolve() == second.resolve():
            return True
        a, b = first.stat(), second.stat()
    except OSError:
        return False
    inode_a = getattr(a, "st_ino", 0)
    inode_b = getattr(b, "st_ino", 0)
    if inode_a and inode_b:
        return inode_a == inode_b and getattr(a, "st_dev", 0) == getattr(b, "st_dev", 0)
    return False


def _user_db_writable(path) -> tuple[bool, str | None]:
    """User DB 独立连接**可写**（`BEGIN IMMEDIATE` + DDL 探针，整体回滚）。

    `BEGIN IMMEDIATE` 会立刻申请写锁：只读文件/只读权限在这里就报错。
    探针建的表在 `ROLLBACK` 后不会留在库里（SQLite 的 DDL 是事务性的），
    所以这个检查对 User DB **无副作用**。
    """
    if not path.exists():
        return False, "USER_DB_MISSING"
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=rw", uri=True)
    except sqlite3.Error as exc:
        return False, f"USER_DB_OPEN_FAILED:{type(exc).__name__}"
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE __s6_write_probe (x INTEGER)")
        connection.execute("ROLLBACK")
        return True, None
    except sqlite3.Error as exc:
        return False, f"USER_DB_NOT_WRITABLE:{type(exc).__name__}"
    finally:
        connection.close()


def _query_only_enabled() -> tuple[bool, str | None]:
    """在新连接上确认 `PRAGMA query_only` 真的返回 1（不是只配了 URI）。"""
    try:
        from app.db import ResearchSessionLocal

        with ResearchSessionLocal() as session:
            value = session.connection().exec_driver_sql("PRAGMA query_only").scalar()
    except Exception as exc:  # noqa: BLE001
        return False, f"QUERY_ONLY_PROBE_FAILED:{type(exc).__name__}"
    return (True, None) if int(value or 0) == 1 else (False, "QUERY_ONLY_DISABLED")


def database_checks() -> dict[str, bool]:
    """灰度数据库门禁的**逐项真实检查**（每次调用都执行，不缓存 PASS）。"""
    settings = get_settings()
    research_path = settings.db_path
    user_path = settings.user_db_path
    checks: dict[str, bool] = {
        "SPLIT_ENABLED": bool(settings.db_split_enabled),
        "RESEARCH_DB_EXISTS": research_path.exists(),
        "RESEARCH_ENGINE_READONLY_URI": False,
        "QUERY_ONLY_ENABLED": False,
        "READONLY_WRITE_PROBE_BLOCKED": False,
        "RESEARCH_FILE_PERMISSION_READONLY": False,
        "USER_DB_EXISTS": user_path.exists(),
        "USER_DB_WRITABLE": False,
        "PATHS_DISTINCT": False,
    }
    if not settings.db_split_enabled:
        return checks

    try:
        from app.db import engine

        checks["RESEARCH_ENGINE_READONLY_URI"] = "mode=ro" in str(engine.url)
    except Exception:  # noqa: BLE001
        checks["RESEARCH_ENGINE_READONLY_URI"] = False

    ok, _reason = _query_only_enabled()
    checks["QUERY_ONLY_ENABLED"] = ok

    probe, _probe_reason = probe_readonly_sqlite(research_path)
    checks["READONLY_WRITE_PROBE_BLOCKED"] = probe == PASS

    checks["RESEARCH_FILE_PERMISSION_READONLY"] = (
        _file_is_readonly(research_path) is True
    )

    writable, _write_reason = _user_db_writable(user_path)
    checks["USER_DB_WRITABLE"] = writable

    checks["PATHS_DISTINCT"] = not _same_file(research_path, user_path)
    return checks


def _database_gate(checks: dict[str, bool] | None = None) -> tuple[str, str | None]:
    """灰度要求应用使用的是**只读** Research DB + 独立可写 User DB。

    仅「文件能被只读打开」不等于应用不会写；这里逐项做**真实运行时检查**
    （不硬编码 PASS，也不缓存结果）：

    1. 连接层拆分已启用；
    2. Research DB 文件存在；
    3. Research 与 User 路径不同、且不是同一 inode；
    4. Research 引擎 URL 含 `mode=ro`；
    5. 新连接 `PRAGMA query_only` 返回 1；
    6. 只读探针（DDL / DML / 写型 PRAGMA）全部被拒绝；
    7. 文件权限本身只读；
    8. User DB 存在且独立连接**可写**（BEGIN IMMEDIATE + DDL 探针后回滚）。

    任何一项不满足都 fail closed —— 缺库、权限异常、路径相同都不会被放行。
    """
    checks = checks if checks is not None else database_checks()
    if not checks["SPLIT_ENABLED"]:
        return FAIL, "DB_SPLIT_NOT_ACTIVE"
    if not checks["RESEARCH_DB_EXISTS"]:
        return FAIL, "RESEARCH_DB_MISSING"
    if not checks["PATHS_DISTINCT"]:
        return FAIL, "DB_PATHS_NOT_DISTINCT"
    if not checks["RESEARCH_ENGINE_READONLY_URI"]:
        return FAIL, "DB_ENGINE_READ_WRITE"
    if not checks["QUERY_ONLY_ENABLED"]:
        return FAIL, "QUERY_ONLY_DISABLED"
    if not checks["READONLY_WRITE_PROBE_BLOCKED"]:
        return FAIL, "READONLY_PROBE_WRITE_SUCCEEDED"
    if not checks["RESEARCH_FILE_PERMISSION_READONLY"]:
        return FAIL, "RESEARCH_FILE_PERMISSION_WRITABLE"
    if not checks["USER_DB_EXISTS"]:
        return FAIL, "USER_DB_MISSING"
    if not checks["USER_DB_WRITABLE"]:
        return FAIL, "USER_DB_NOT_WRITABLE"
    return PASS, None


def _scheduler_gate() -> tuple[str, str | None]:
    try:
        from app.jobs import scheduler as scheduler_module

        scheduler = scheduler_module.get_scheduler()
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"SCHEDULER_UNAVAILABLE:{type(exc).__name__}"
    if scheduler is None:
        return PASS, None
    running = bool(getattr(scheduler, "_scheduler", None) and scheduler._scheduler.running)
    return (FAIL, "SCHEDULER_RUNNING") if running else (PASS, None)


def _external_fetch_gate() -> tuple[str, str | None]:
    if external_guard.enabled():
        return FAIL, "EXTERNAL_FETCH_ENABLED"
    return PASS, None


def _startup_gate() -> tuple[str, str | None]:
    if not _startup["recorded"]:
        return FAIL, "STARTUP_NOT_RECORDED"
    if _startup["config_errors"]:
        return FAIL, ",".join(_startup["config_errors"])
    return PASS, None


def readiness_snapshot() -> dict[str, Any]:
    """计算 readiness。每次请求都重新计算，不缓存 —— 避免「启动时检查一次就永久绿」。"""
    gates: dict[str, str] = {}
    reasons: dict[str, str] = {}

    # 数据库检查只跑一次（含 User DB 的 BEGIN IMMEDIATE 探针），门禁与明细共用
    db_checks = database_checks()

    for name, probe in (
        ("STARTUP_CONFIG_VALID", _startup_gate),
        ("EGRESS_APP_POLICY_LOCKED", _egress_app_gate),
        ("DATABASE_READ_ONLY_VERIFIED", lambda: _database_gate(db_checks)),
        ("SCHEDULER_DISABLED", _scheduler_gate),
        ("EXTERNAL_FETCH_DISABLED", _external_fetch_gate),
    ):
        state, reason = probe()
        gates[name] = state
        if reason:
            reasons[name] = reason

    # OS 级策略本轮无法验证：固定 NOT_VERIFIED（不允许被环境变量伪造）
    gates["EGRESS_OS_POLICY_VERIFIED"] = NOT_VERIFIED
    reasons["EGRESS_OS_POLICY_VERIFIED"] = "NO_ISOLATED_OS_NETWORK_ACCESS"

    blocking = [name for name, state in gates.items() if state != PASS]
    gates["CANARY_READINESS"] = PASS if not blocking else FAIL

    policy = egress_policy.policy_status()
    return {
        "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "ready": gates["CANARY_READINESS"] == PASS,
        "status": "ready" if gates["CANARY_READINESS"] == PASS else "not_ready",
        "gates": gates,
        "blocking": blocking,
        "reasons": reasons,
        "policy": {
            "loaded": policy.get("loaded"),
            "error": policy.get("error"),
            "entries": policy.get("entries", 0),
        },
        # 逐项布尔检查（**不含路径**）：便于运维定位是哪一道没通过，
        # 同时不泄露服务器文件系统结构。
        "database_checks": db_checks,
        "notes": [
            "health 只表示进程存活；ready 表示灰度必要门禁全部 PASS",
            "EGRESS_OS_POLICY_VERIFIED 需要独立网络命名空间/防火墙实测，本轮未执行",
        ],
    }
