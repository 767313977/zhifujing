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


def _database_gate() -> tuple[str, str | None]:
    """灰度要求应用使用的是**只读** Research DB + 独立 User DB。

    仅「文件能被只读打开」不等于应用不会写；必须同时满足：
    1. `FUPAN_DB_SPLIT_ENABLED=true`（连接层拆分已启用）；
    2. `FUPAN_RESEARCH_DB_READONLY=true`（运维显式确认研究库只读形态）；
    3. Research 引擎 URL 含 `mode=ro`；
    4. 只读探针通过。
    """
    settings = get_settings()
    if not settings.db_split_enabled or not settings.research_db_readonly:
        return FAIL, "DB_SPLIT_NOT_ACTIVE"
    try:
        from app.db import engine

        url = str(engine.url)
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"ENGINE_UNAVAILABLE:{type(exc).__name__}"
    probe, reason = probe_readonly_sqlite(settings.db_path)
    if probe != PASS:
        return FAIL, f"READONLY_PROBE_{reason}"
    if "mode=ro" not in url:
        return FAIL, "DB_ENGINE_READ_WRITE"
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

    for name, probe in (
        ("STARTUP_CONFIG_VALID", _startup_gate),
        ("EGRESS_APP_POLICY_LOCKED", _egress_app_gate),
        ("DATABASE_READ_ONLY_VERIFIED", _database_gate),
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
        "notes": [
            "health 只表示进程存活；ready 表示灰度必要门禁全部 PASS",
            "EGRESS_OS_POLICY_VERIFIED 需要独立网络命名空间/防火墙实测，本轮未执行",
        ],
    }
