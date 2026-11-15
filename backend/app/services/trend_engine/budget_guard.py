"""M6.6 iFinD 请求预算守卫与用途闸门。

硬约束：
- 第一轮上限固定 100 次，运行时不可提高；
- 预占计数在发请求之前完成，失败/超时/重试都不回滚；
- 账本损坏或无法解析时 fail closed，禁止继续请求；
- 只允许调用白名单内的工具；
- 请求哈希与日志不得包含 Token；
- 网站商业许可未建立时，禁止任何发布/导出动作。

本模块纯本地（文件锁 + JSONL），不发起网络请求。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

FIXED_LIMIT = 100
TOKEN_PATTERNS = (
    re.compile(r"bearer\s+[a-z0-9\-._~+/]+=*", re.IGNORECASE),
    re.compile(
        r"(api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token)\s*[:=]\s*[^\s,;]+",
        re.IGNORECASE,
    ),
    re.compile(r"authorization\s*[:=]\s*[^\s,;]+", re.IGNORECASE),
)


class BudgetExhausted(RuntimeError):
    """预算已用尽，必须硬停止。"""


class BudgetLedgerCorrupt(RuntimeError):
    """账本缺失/损坏，fail closed。"""


class ToolNotAllowed(RuntimeError):
    """工具不在白名单，拒绝调用。"""


class WebsiteLicenseNotEstablished(RuntimeError):
    """网站商业许可未建立，禁止发布/导出。"""


def redact(text: str) -> str:
    """把可能的凭据替换掉，用于日志/异常。"""
    redacted = text
    for pattern in TOKEN_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def assert_no_credentials(text: str) -> None:
    for pattern in TOKEN_PATTERNS:
        if pattern.search(text):
            raise ValueError("检测到疑似凭据，禁止写入日志/账本")


def request_hash(tool: str, params: dict) -> str:
    payload = json.dumps({"tool": tool, "params": params}, ensure_ascii=False, sort_keys=True, default=str)
    assert_no_credentials(payload)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest().upper()


@dataclass(frozen=True)
class Reservation:
    request_id: str
    sequence_no: int
    tool_name: str
    purpose: str


class RequestBudgetGuard:
    """JSONL 账本 + 独占文件锁；任何异常都不释放已预占名额。"""

    def __init__(
        self,
        ledger_path: Path,
        *,
        limit: int = FIXED_LIMIT,
        allowed_tools: set[str] | frozenset[str] | None = None,
        lock_timeout: float = 10.0,
    ) -> None:
        if limit != FIXED_LIMIT:
            raise ValueError(f"本轮预算固定为 {FIXED_LIMIT}，不允许改为 {limit}")
        self.path = Path(ledger_path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.limit = limit
        self.allowed_tools = frozenset(allowed_tools) if allowed_tools else None
        self.lock_timeout = lock_timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_header()

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        deadline = time.monotonic() + self.lock_timeout
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise BudgetExhausted("预算锁等待超时，fail closed")
                time.sleep(0.02)
        try:
            yield
        finally:
            try:
                os.unlink(self.lock_path)
            except FileNotFoundError:
                pass

    def _ensure_header(self) -> None:
        if not self.path.exists():
            header = {
                "type": "header",
                "limit": self.limit,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "batch": self.path.parent.name,
            }
            self.path.write_text(json.dumps(header, ensure_ascii=False) + "\n", encoding="utf-8")
            return
        try:
            first = self.path.read_text(encoding="utf-8").splitlines()[0]
            header = json.loads(first)
        except (IndexError, json.JSONDecodeError) as exc:
            raise BudgetLedgerCorrupt(f"账本头部不可解析：{self.path}") from exc
        if header.get("type") != "header" or int(header.get("limit", -1)) != self.limit:
            raise BudgetLedgerCorrupt(f"账本头部不匹配：{header}")
        for line in self.path.read_text(encoding="utf-8").splitlines()[1:]:
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError as exc:
                raise BudgetLedgerCorrupt(f"账本行损坏：{line[:80]}") from exc

    def _entries(self) -> list[dict]:
        lines = self.path.read_text(encoding="utf-8").splitlines()[1:]
        return [json.loads(line) for line in lines if line.strip()]

    @property
    def used(self) -> int:
        return sum(1 for entry in self._entries() if entry.get("type") == "reserve")

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)

    def reserve(
        self,
        *,
        tool_name: str,
        endpoint: str = "",
        purpose: str = "G9",
        sample_ids: tuple[str, ...] = (),
        params_hash: str = "",
    ) -> Reservation:
        if purpose not in {"CAPABILITY", "G9", "G3", "RETRY"}:
            raise ValueError(f"未知 purpose：{purpose}")
        if self.allowed_tools is not None and tool_name not in self.allowed_tools:
            raise ToolNotAllowed(f"{tool_name} 不在本轮白名单")
        assert_no_credentials(params_hash)
        with self._exclusive():
            used = self.used
            if used + 1 > self.limit:
                entry = {
                    "type": "rejected",
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "tool_name": tool_name,
                    "reason": "BUDGET_EXHAUSTED",
                }
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                raise BudgetExhausted(f"预算已用尽：{used}/{self.limit}")
            sequence = used + 1
            request_id = f"r{sequence:03d}-{uuid.uuid4().hex[:8]}"
            entry = {
                "type": "reserve",
                "batch_id": self.path.parent.name,
                "request_id": request_id,
                "sequence_no": sequence,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "tool_name": tool_name,
                "endpoint": endpoint,
                "purpose": purpose,
                "sample_ids": list(sample_ids),
                "request_hash": params_hash,
                "budget_reserved": True,
            }
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return Reservation(request_id, sequence, tool_name, purpose)

    def record(
        self,
        reservation: Reservation,
        *,
        response_status: str,
        provider_error_code: str = "",
        elapsed_ms: int = 0,
        returned_rows: int = 0,
        returned_cells: int = 0,
        payload_hash: str = "",
        storage_status: str = "SUMMARY_ONLY",
        retry_of: str = "",
    ) -> dict:
        entry = {
            "type": "result",
            "request_id": reservation.request_id,
            "sequence_no": reservation.sequence_no,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "tool_name": reservation.tool_name,
            "purpose": reservation.purpose,
            "response_status": response_status,
            "provider_error_code": provider_error_code,
            "elapsed_ms": elapsed_ms,
            "returned_rows": returned_rows,
            "returned_cells": returned_cells,
            "payload_hash": payload_hash,
            "storage_status": storage_status,
            "retry_of": retry_of,
        }
        assert_no_credentials(json.dumps(entry, ensure_ascii=False))
        with self._exclusive():
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def summary(self) -> dict[str, Any]:
        entries = self._entries()
        reserves = [item for item in entries if item.get("type") == "reserve"]
        results = [item for item in entries if item.get("type") == "result"]
        by_purpose: dict[str, int] = {}
        for item in reserves:
            by_purpose[item.get("purpose", "")] = by_purpose.get(item.get("purpose", ""), 0) + 1
        return {
            "limit": self.limit,
            "used": len(reserves),
            "remaining": max(self.limit - len(reserves), 0),
            "by_purpose": by_purpose,
            "results_recorded": len(results),
            "rejected": sum(1 for item in entries if item.get("type") == "rejected"),
        }


def payload_persistence_policy(license_confirmed: bool) -> str:
    """未获本地存储许可时只允许保存摘要；返回策略标记。"""
    return "ALLOW_RAW" if license_confirmed else "SUMMARY_ONLY"


def assert_website_publish_allowed(ledger_rows: list[dict], *, action: str) -> None:
    """网站商业许可未建立时，任何发布/导出动作都必须被拒绝。"""
    if any(row.get("public_display_allowed") == "ALLOWED" for row in ledger_rows):
        return
    raise WebsiteLicenseNotEstablished(f"{action} 被拒绝：WEBSITE_COMMERCIAL_LICENSE = NOT_ESTABLISHED")
