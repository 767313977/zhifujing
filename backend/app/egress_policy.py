"""统一出站策略（S6-A1）：**默认拒绝**，只放行显式允许的目标。

为什么需要它：`external_guard` 之前只区分「在线 / 离线」两种状态 —— 在线时任何
目标都放行，等于把安全边界完全交给「记得把开关关掉」。S6 要求
`APP_EGRESS_ENABLED = ALLOW_ONLY_EXPLICIT_POLICY`：在线也要按允许名单逐条判断，
未知目标、未知协议、未知端口一律拒绝。

设计约束（对应任务书 A1）：

1. `DEFAULT = DENY`：名单里没有的一律拒绝；
2. loopback 只在策略显式声明时放行（见策略文件的 `loopback` 段）；
3. 不能用整个网段 / 高端口代替具体目标；
4. 走代理不等于放行 —— 代理主机本身也要在名单里；
5. **IP literal 不能绕过域名规则**：除严格回环外，直接连 IP 一律拒绝；
6. 策略文件缺失、格式错误、版本不匹配 → fail closed（全部拒绝），
   并在 `policy_status()` 里给出稳定错误码。

⚠️ 这是**应用层**策略。它不能证明操作系统或网络层已经阻断出站；
OS 级验证见 `reports/s6/security/os-policy-verification.md`（本轮为 NOT_VERIFIED）。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.config import ROOT

logger = logging.getLogger(__name__)

#: 策略文件默认位置（随仓库分发，便于审计与 code review）
DEFAULT_POLICY_FILE = ROOT / "deploy" / "s6" / "egress-policy.json"

#: 支持的最高策略版本；文件里写更高的版本视为「策略版本不匹配」→ fail closed
SUPPORTED_VERSION = 1

_lock = threading.Lock()
_cache: dict[str, Any] | None = None
_cache_signature: tuple[str, float, int] | None = None
_load_error: str | None = None


class EgressPolicyError(RuntimeError):
    """策略加载 / 校验失败。调用方应 fail closed（拒绝出站）。"""

    def __init__(self, reason_code: str, detail: str = ""):
        self.reason_code = reason_code
        super().__init__(f"出站策略不可用（{reason_code}）{('：' + detail) if detail else ''}")


def _normalize_host(value: Any) -> str:
    """把 URL / host:port / 裸主机名统一成小写主机名；失败返回空串。"""
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("ascii", "ignore")
        except Exception:  # noqa: BLE001
            return ""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text:
        try:
            text = urlsplit(text).hostname or ""
        except ValueError:
            return ""
    if text.startswith("[") and "]" in text:
        text = text[1 : text.index("]")]
    elif text.count(":") == 1:
        text = text.split(":", 1)[0]
    return text.strip().rstrip(".").lower()


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _validate(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise EgressPolicyError("POLICY_INVALID_ROOT")
    version = payload.get("version")
    if version != SUPPORTED_VERSION:
        raise EgressPolicyError(
            "POLICY_VERSION_MISMATCH", f"期望 {SUPPORTED_VERSION}，实际 {version!r}"
        )
    if str(payload.get("default", "")).lower() != "deny":
        raise EgressPolicyError("POLICY_DEFAULT_NOT_DENY")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise EgressPolicyError("POLICY_NO_ENTRIES")

    normalized: list[dict] = []
    seen: set[str] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            raise EgressPolicyError("POLICY_ENTRY_NOT_OBJECT")
        entry_id = str(raw.get("id") or "").strip()
        host_suffix = _normalize_host(raw.get("host_suffix"))
        ports = raw.get("ports")
        if not entry_id or not host_suffix or not isinstance(ports, list) or not ports:
            raise EgressPolicyError("POLICY_ENTRY_INCOMPLETE", entry_id or "<no-id>")
        if entry_id in seen:
            raise EgressPolicyError("POLICY_DUPLICATE_ENTRY", entry_id)
        seen.add(entry_id)
        clean_ports: list[int] = []
        for port in ports:
            if not isinstance(port, int) or not (0 < port < 65536):
                raise EgressPolicyError("POLICY_BAD_PORT", f"{entry_id}={port!r}")
            clean_ports.append(port)
        if _is_ip_literal(host_suffix):
            # 名单必须是域名；IP 一律拒绝，避免「用 IP literal 绕过域名规则」
            raise EgressPolicyError("POLICY_IP_ENTRY_FORBIDDEN", entry_id)
        normalized.append(
            {
                "id": entry_id,
                "purpose": str(raw.get("purpose") or ""),
                "host_suffix": host_suffix,
                "ports": sorted(set(clean_ports)),
                "protocols": [str(item).lower() for item in raw.get("protocols") or ["tcp"]],
                "environments": [str(item).lower() for item in raw.get("environments") or []],
                "enabled": bool(raw.get("enabled", True)),
            }
        )

    loopback = payload.get("loopback") or {}
    if not isinstance(loopback, dict):
        raise EgressPolicyError("POLICY_BAD_LOOPBACK")

    raw_proxies = payload.get("proxies", [])
    if not isinstance(raw_proxies, list):
        raise EgressPolicyError("POLICY_BAD_PROXIES")
    proxies: list[dict] = []
    for raw in raw_proxies:
        if not isinstance(raw, dict):
            raise EgressPolicyError("POLICY_BAD_PROXIES")
        host_suffix = _normalize_host(raw.get("host_suffix"))
        ports = raw.get("ports") or []
        if not host_suffix or _is_ip_literal(host_suffix) or not isinstance(ports, list):
            raise EgressPolicyError("POLICY_BAD_PROXIES")
        proxies.append(
            {
                "id": str(raw.get("id") or f"P-{len(proxies) + 1}"),
                "purpose": str(raw.get("purpose") or ""),
                "host_suffix": host_suffix,
                "ports": sorted({int(port) for port in ports if isinstance(port, int)}),
                "environments": [str(item).lower() for item in raw.get("environments") or []],
            }
        )

    return {
        "version": version,
        "default": "deny",
        "loopback": {
            "allow": bool(loopback.get("allow", False)),
            "allow_any_port": bool(loopback.get("allow_any_port", False)),
            "ports": sorted({int(p) for p in loopback.get("ports") or []}),
        },
        "ip_literals": str(payload.get("ip_literals") or "deny_except_loopback"),
        "proxies": proxies,
        "entries": normalized,
    }


def load_policy(path: str | Path | None = None, *, force: bool = False) -> dict:
    """加载并校验策略；失败抛 `EgressPolicyError`（调用方必须按拒绝处理）。"""
    global _cache, _cache_signature, _load_error
    target = Path(path) if path else DEFAULT_POLICY_FILE
    try:
        stat = target.stat()
        signature = (str(target.resolve()), stat.st_mtime, stat.st_size)
    except OSError as exc:
        _load_error = "POLICY_FILE_MISSING"
        raise EgressPolicyError("POLICY_FILE_MISSING", str(exc)) from exc

    with _lock:
        if not force and _cache is not None and _cache_signature == signature:
            return _cache
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        validated = _validate(payload)
    except EgressPolicyError as exc:
        _load_error = exc.reason_code
        raise
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _load_error = "POLICY_PARSE_ERROR"
        raise EgressPolicyError("POLICY_PARSE_ERROR", str(exc)) from exc

    with _lock:
        _cache = validated
        _cache_signature = signature
        _load_error = None
    return validated


def reset_policy_cache() -> None:
    """测试用：清空缓存，让下一次判断重新读文件。"""
    global _cache, _cache_signature, _load_error
    with _lock:
        _cache = None
        _cache_signature = None
        _load_error = None


def policy_status(path: str | Path | None = None) -> dict:
    """策略健康度（给 readiness / 审计用）。不抛异常。"""
    try:
        policy = load_policy(path)
    except EgressPolicyError as exc:
        return {
            "loaded": False,
            "error": exc.reason_code,
            "file": str(Path(path) if path else DEFAULT_POLICY_FILE),
            "entries": 0,
        }
    return {
        "loaded": True,
        "error": None,
        "file": str(Path(path) if path else DEFAULT_POLICY_FILE),
        "version": policy["version"],
        "entries": len(policy["entries"]),
        "enabled_entries": sum(1 for item in policy["entries"] if item["enabled"]),
        "loopback_allow": policy["loopback"]["allow"],
        "loopback_allow_any_port": policy["loopback"]["allow_any_port"],
    }


def _loopback_allowed(port: int | None) -> bool:
    policy = load_policy()
    loopback = policy["loopback"]
    if not loopback["allow"]:
        return False
    if loopback["allow_any_port"]:
        return True
    if port is None:
        return False
    return int(port) in loopback["ports"]


def host_allowed(host: Any, port: int | None = None, *, environment: str | None = None) -> bool:
    """判断目标是否被允许。**任何异常都返回 False**（fail closed）。"""
    try:
        normalized = _normalize_host(host)
        if not normalized:
            return False
        if _is_ip_literal(normalized):
            address = ipaddress.ip_address(normalized)
            return address.is_loopback and _loopback_allowed(port)
        if normalized in {"localhost", "ip6-localhost"}:
            return _loopback_allowed(port)
        policy = load_policy()
        for entry in policy["entries"]:
            if not entry["enabled"]:
                continue
            if environment and entry["environments"] and environment.lower() not in entry["environments"]:
                continue
            suffix = entry["host_suffix"]
            if normalized != suffix and not normalized.endswith("." + suffix):
                continue
            if port is None or int(port) in entry["ports"]:
                return True
        return False
    except Exception:  # noqa: BLE001 - 策略不可用必须 fail closed
        return False


def proxies_allowed() -> bool:
    """本轮策略是否显式授权了代理出口（默认没有）。

    `False` 时 `external_guard` 会把 `urllib.request.getproxies()`（含系统代理与
    `HTTP(S)_PROXY`）过滤成空，避免「应用直连被拦、但从本机代理绕出去」。
    """
    try:
        return bool(load_policy()["proxies"])
    except EgressPolicyError:
        return False


def describe_denial(host: Any, port: int | None = None) -> str:
    """给日志用的稳定原因码，**不含完整 URL / query / 凭据**。"""
    normalized = _normalize_host(host)
    if not normalized:
        return "UNKNOWN_DESTINATION"
    if _is_ip_literal(normalized):
        return "IP_LITERAL_DENIED"
    if normalized in {"localhost", "ip6-localhost"}:
        return "LOOPBACK_PORT_NOT_ALLOWED"
    try:
        policy = load_policy()
    except EgressPolicyError as exc:
        return exc.reason_code
    for entry in policy["entries"]:
        suffix = entry["host_suffix"]
        if normalized == suffix or normalized.endswith("." + suffix):
            return "PORT_NOT_ALLOWED"
    return "UNKNOWN_DESTINATION"


def allowed_hosts() -> list[dict]:
    """允许名单摘要（**不含任何凭据**），用于审计材料。"""
    try:
        policy = load_policy()
    except EgressPolicyError:
        return []
    return [
        {
            "id": entry["id"],
            "purpose": entry["purpose"],
            "host_suffix": entry["host_suffix"],
            "ports": entry["ports"],
            "protocols": entry["protocols"],
            "environments": entry["environments"],
            "enabled": entry["enabled"],
        }
        for entry in policy["entries"]
    ]
