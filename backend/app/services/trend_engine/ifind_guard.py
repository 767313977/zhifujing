"""iFinD 接入护栏：预算、限速、错误与 schema 校验。

M6.4 要求：
- 单函数 QPS ≤2、账号总并发 ≤3；
- 每个请求前估算、返回后记账，达到 100% 预算必须硬停止；
- 供应商错误码 / 空表不能被当作「无数据」；
- 字段缺失必须标记 `SCHEMA_MISMATCH`；
- 日期、代码映射严格校验；
- payload 哈希稳定，`available_at` 缺失不得伪造。

本模块为纯函数/纯内存实现，不发起网络请求。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    """预算用尽或请求会使预算超限。"""


class RateLimitExceeded(RuntimeError):
    """超出配置的 QPS。"""


class SchemaMismatch(ValueError):
    """供应商返回字段与预期不一致。"""


class ProviderError(RuntimeError):
    """供应商返回错误码；调用方不得把它当成空数据。"""


@dataclass
class RequestBudget:
    limit_cells: int
    used_cells: int = 0
    calls: int = 0
    hard_stop_ratio: float = 1.0
    warn_ratio: float = 0.8
    events: list[str] = field(default_factory=list)

    def check(self, estimated_cells: int) -> None:
        if estimated_cells < 0:
            raise ValueError("estimated_cells 不能为负")
        if self.used_cells + estimated_cells > self.limit_cells * self.hard_stop_ratio:
            raise BudgetExceeded(
                f"预算超限：已用 {self.used_cells} + 预估 {estimated_cells} > {self.limit_cells}"
            )
        if (self.used_cells + estimated_cells) >= self.limit_cells * self.warn_ratio:
            self.events.append(f"BUDGET_WARN:{self.used_cells + estimated_cells}/{self.limit_cells}")

    def record(self, actual_cells: int) -> None:
        self.check(actual_cells)
        self.used_cells += actual_cells
        self.calls += 1

    @property
    def remaining(self) -> int:
        return max(self.limit_cells - self.used_cells, 0)


class RateLimiter:
    """简单令牌桶：min_interval 秒/次，容量固定 1，保证串行。"""

    def __init__(self, qps: float = 2.0, clock=time.monotonic, sleep=time.sleep) -> None:
        if qps <= 0:
            raise ValueError("qps 必须为正")
        self.min_interval = 1.0 / qps
        self._clock = clock
        self._sleep = sleep
        self._last = 0.0

    def acquire(self) -> None:
        now = self._clock()
        wait = self.min_interval - (now - self._last)
        if wait > 0:
            self._sleep(wait)
            now = self._clock()
        self._last = now


def ensure_no_provider_error(payload) -> None:
    """供应商错误码 / 错误消息必须抛出，不能返回空数据。"""
    if isinstance(payload, dict):
        for key in ("error", "errmsg", "errno", "code", "__error__"):
            if key in payload and payload[key] not in (None, "", 0, "0"):
                raise ProviderError(f"iFinD 返回错误：{key}={str(payload[key])[:120]}")
        if payload.get("ok") is False:
            raise ProviderError("iFinD 返回 ok=false")


def require_fields(rows: list[dict], required: tuple[str, ...], *, context: str = "") -> None:
    if not rows:
        raise SchemaMismatch(f"{context} 返回空表，不能当作有效数据")
    missing = [field for field in required if not any(field in row for row in rows)]
    if missing:
        raise SchemaMismatch(f"{context} 缺少字段：{missing}")


def normalize_code(value: str) -> str:
    text = str(value or "").strip()
    if "." in text:
        code, suffix = text.split(".", 1)
        if suffix.upper() not in {"SH", "SZ", "BJ"}:
            raise ValueError(f"未知交易所后缀：{value}")
        return f"{code.zfill(6)}.{suffix.upper()}"
    if not text.isdigit() or len(text) != 6:
        raise ValueError(f"代码格式非法：{value}")
    return text


def normalize_date(value: str) -> str:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text
    raise ValueError(f"日期格式非法：{value}")


def payload_hash(payload) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest().upper()


def available_at_or_null(value: str | None) -> str | None:
    """供应商未提供可得时间时必须返回 NULL，禁止用交易日顶替。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
