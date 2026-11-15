"""M6.7 供应商授权守卫与 Fake Provider（纯本地，无网络）。

规则（fail closed）：
1. 无台账行 → 拒绝；
2. `NOT_AUTHORIZED` / `REJECTED` / `UNKNOWN` / `EXPIRED` → 拒绝；
3. 证据 URI 缺失 → 拒绝；
4. 许可过期 → 拒绝；
5. 用途与许可不匹配 → 拒绝（个人研究 ≠ 网站内部使用 ≠ 公开展示 ≠ 再分发）；
6. 允许 API 调用 ≠ 允许本地存储；
7. 允许本地存储 ≠ 允许公开展示；
8. 允许衍生指标 ≠ 允许再分发；
9. 未授权时网络调用计数必须为 0（Fake Provider 永不联网）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

ALLOWED_STATUS = {"AUTHORIZED_LIMITED", "AUTHORIZED_FULL"}
DENY_STATUS = {"NOT_AUTHORIZED", "REJECTED", "UNKNOWN", "EXPIRED", "PENDING_VENDOR_REPLY", "PENDING_USER_APPROVAL"}
PURPOSES = {"PERSONAL_RESEARCH", "INTERNAL_WEBSITE", "PUBLIC_DISPLAY", "REDISTRIBUTION", "DERIVED_METRICS"}
STORAGE_MODES = {"NONE", "SUMMARY_ONLY", "RAW_LOCAL"}


class AuthorizationDenied(RuntimeError):
    """授权不足，调用必须被拒绝。"""


@dataclass(frozen=True)
class AuthorizationRecord:
    provider: str
    dataset: str
    approval_status: str
    evidence_uri: str = ""
    approved_by: str = ""
    approval_date: str = ""
    expires_at: str | None = None
    local_storage_allowed: str = "NOT_ALLOWED"
    derived_metrics_allowed: str = "NOT_ALLOWED"
    website_internal_use_allowed: str = "NOT_ALLOWED"
    public_display_allowed: str = "NOT_ALLOWED"
    redistribution_allowed: str = "NOT_ALLOWED"
    api_permission_status: str = "UNKNOWN"
    quota_and_rate_limit: str = ""
    retention_period: str = "UNKNOWN"


@dataclass
class FakeProvider:
    """只返回人工构造 fixture；任何方法都不会发起网络请求。"""

    provider: str
    dataset: str
    capability: tuple[str, ...] = ()
    network_calls: int = 0
    fixture: dict[str, Any] = field(default_factory=dict)

    def capabilities(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "dataset": self.dataset,
            "data_origin": "SYNTHETIC_FIXTURE",
            "network_enabled": False,
        }

    def fetch(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "dataset": self.dataset,
            "data_origin": "SYNTHETIC_FIXTURE",
            "records": list(self.fixture.get("records", [])),
            "network_calls": self.network_calls,
        }


class AuthorizationGuard:
    """按 provider × dataset × purpose × storage 逐维判断。"""

    def __init__(self, records: list[AuthorizationRecord]) -> None:
        self._records = {(record.provider, record.dataset): record for record in records}

    def get(self, provider: str, dataset: str) -> AuthorizationRecord | None:
        return self._records.get((provider, dataset))

    def check(
        self,
        *,
        provider: str,
        dataset: str,
        purpose: str,
        storage: str = "NONE",
        today: date | None = None,
    ) -> dict[str, Any]:
        if purpose not in PURPOSES:
            raise ValueError(f"未知用途：{purpose}")
        if storage not in STORAGE_MODES:
            raise ValueError(f"未知存储模式：{storage}")

        record = self.get(provider, dataset)
        if record is None:
            return self._deny("NO_LEDGER_ROW")
        if record.approval_status in DENY_STATUS:
            return self._deny(f"STATUS_{record.approval_status}")
        if record.approval_status not in ALLOWED_STATUS:
            return self._deny(f"STATUS_{record.approval_status}")
        if not record.evidence_uri:
            return self._deny("EVIDENCE_URI_MISSING")
        if not record.approved_by or not record.approval_date:
            return self._deny("APPROVER_OR_DATE_MISSING")

        current = today or date.today()
        if record.expires_at:
            try:
                expiry = date.fromisoformat(str(record.expires_at)[:10])
            except ValueError:
                return self._deny("EXPIRY_UNPARSABLE")
            if expiry < current:
                return self._deny("EXPIRED")

        if storage == "RAW_LOCAL" and record.local_storage_allowed != "ALLOWED":
            return self._deny("RAW_STORAGE_NOT_ALLOWED")
        if purpose == "DERIVED_METRICS" and record.derived_metrics_allowed != "ALLOWED":
            return self._deny("DERIVED_METRICS_NOT_ALLOWED")
        if purpose == "INTERNAL_WEBSITE" and record.website_internal_use_allowed != "ALLOWED":
            return self._deny("INTERNAL_WEBSITE_NOT_ALLOWED")
        if purpose == "PUBLIC_DISPLAY" and record.public_display_allowed != "ALLOWED":
            return self._deny("PUBLIC_DISPLAY_NOT_ALLOWED")
        if purpose == "REDISTRIBUTION" and record.redistribution_allowed != "ALLOWED":
            return self._deny("REDISTRIBUTION_NOT_ALLOWED")
        if purpose == "PERSONAL_RESEARCH" and record.approval_status not in ALLOWED_STATUS:
            return self._deny("PERSONAL_RESEARCH_NOT_ALLOWED")

        return {
            "allowed": True,
            "reason": "",
            "provider": provider,
            "dataset": dataset,
            "purpose": purpose,
            "storage": storage,
            "approval_status": record.approval_status,
        }

    def require(self, **kwargs: Any) -> dict[str, Any]:
        decision = self.check(**kwargs)
        if not decision["allowed"]:
            raise AuthorizationDenied(decision["reason"])
        return decision

    @staticmethod
    def _deny(reason: str) -> dict[str, Any]:
        return {"allowed": False, "reason": reason}


def summarize_decisions(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    allowed = sum(1 for item in decisions if item.get("allowed"))
    return {"total": len(decisions), "allowed": allowed, "denied": len(decisions) - allowed}
