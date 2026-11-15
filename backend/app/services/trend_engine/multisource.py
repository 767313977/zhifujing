"""M6.5 多供应商协议与 staging 护栏。

设计目标（对应任务书第 7 章与第 10 章测试要求）：

- 统一 `CorporateActionProvider` / `SectorMembershipProvider` 协议，未接入的供应商用
  `NotImplementedProvider`，任何方法都抛错，**不伪造成功返回**；
- 未授权供应商在注册表入口被硬拒绝，不会走到「返回空列表」；
- 供应商错误码不得被当作空数据；
- staging 只追加、按 `provider` 隔离，多供应商同一事件互不覆盖；
- `available_at` 缺失写 NULL，不得用交易日/`ingested_at` 顶替；
- 公告日期只保留日期级，不得补 `T00:00:00` 伪装时间戳；
- `SNAPSHOT_ONLY` 不得携带 in/out 区间；
- 板块名称模糊匹配不能自动成为已批准映射；
- 禁止用当前快照回填历史成分；
- staging 路径必须在批次目录内，禁止指向四个原始研究库。

本模块纯内存实现，不发起任何网络请求。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

KNOWN_PROVIDERS: frozenset[str] = frozenset(
    {"iFinD", "Tushare", "Lixinger", "CnOpenData", "Choice", "JoinQuant"}
)

#: 本阶段唯一已获数据调用授权的供应商（且仅限 M6.4 已批准的最小范围）。
DEFAULT_AUTHORIZED: frozenset[str] = frozenset({"iFinD"})

PROTECTED_DB_NAMES: frozenset[str] = frozenset(
    {"b1_research.db", "history.db", "b2_final_snapshot.db", "fupan.db"}
)

SNAPSHOT_ONLY = "SNAPSHOT_ONLY"
INTERVAL = "INTERVAL"

OFFICIAL_MAPPING_METHODS: frozenset[str] = frozenset(
    {"official_code_table", "manual_reviewed", "exact_code_match"}
)
CANDIDATE_MAPPING_METHODS: frozenset[str] = frozenset(
    {"name_exact_candidate", "name_fuzzy_candidate", "name_unverified"}
)


class ProviderNotAuthorized(RuntimeError):
    """供应商未获授权，调用必须硬失败。"""


class ProviderNotImplemented(RuntimeError):
    """适配器尚未接入，禁止返回伪成功。"""


class ProviderError(RuntimeError):
    """供应商返回错误；调用方不得将其当作空数据。"""


class StagingViolation(RuntimeError):
    """违反 staging / 数据库保护约束。"""


class MappingNotApproved(RuntimeError):
    """板块映射尚未批准，不能用于归一化。"""


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    datasets: tuple[str, ...] = ()
    history_start: str | None = None
    pit_precision: str = "date"
    supports_revision: bool = False
    license_scope: str = "NOT_AUTHORIZED"
    notes: str = ""

    def __post_init__(self) -> None:
        if self.provider not in KNOWN_PROVIDERS:
            raise ValueError(f"未知供应商：{self.provider}")
        if self.pit_precision not in {"date", "timestamp", "none"}:
            raise ValueError(f"pit_precision 非法：{self.pit_precision}")

    def to_row(self, batch_id: str) -> dict[str, Any]:
        return {
            "batch_id": batch_id,
            "provider": self.provider,
            "datasets": ",".join(self.datasets),
            "history_start": self.history_start,
            "pit_precision": self.pit_precision,
            "supports_revision": int(self.supports_revision),
            "license_scope": self.license_scope,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class CorporateActionRequest:
    provider: str
    codes: tuple[str, ...]
    start_date: str
    end_date: str


@dataclass(frozen=True)
class FactorRequest:
    provider: str
    codes: tuple[str, ...]
    start_date: str
    end_date: str


@dataclass(frozen=True)
class SectorSnapshotRequest:
    provider: str
    sector_ids: tuple[str, ...]
    snapshot_date: str


@dataclass(frozen=True)
class MembershipEventRequest:
    provider: str
    sector_ids: tuple[str, ...]
    start_date: str
    end_date: str


@dataclass(frozen=True)
class ProviderResult:
    """一次供应商返回。

    `records` 为空时必须由调用方显式声明 `empty_confirmed=True`，否则视为解析失败——
    供应商错误、限流、字段变更都不能以「空数据」形式静默通过。
    """

    provider: str
    dataset: str
    endpoint: str
    records: tuple[dict[str, Any], ...]
    raw_payload: Any
    source_version: str | None = None
    available_at: str | None = None
    quality_status: str = "OK"
    license_scope: str = "NOT_AUTHORIZED"
    empty_confirmed: bool = False
    payload_hash: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if not self.records and not self.empty_confirmed:
            raise ProviderError(
                f"{self.provider}/{self.dataset} 返回空结果但未确认：不得当作无数据"
            )
        object.__setattr__(self, "payload_hash", payload_hash(self.raw_payload))


def payload_hash(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest().upper()


def normalize_available_at(value: str | None) -> str | None:
    """供应商未提供可得时间时返回 NULL，禁止用交易日或 ingested_at 顶替。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_DATE_RE = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def normalize_announcement(value: str | None) -> tuple[str | None, str]:
    """公告时间归一化：只输出日期级，绝不补 `T00:00:00` 冒充时间戳。

    返回 `(yyyy-mm-dd, "date")`；缺失时返回 `(None, "none")`。
    """
    if value is None:
        return None, "none"
    text = str(value).strip()
    if not text:
        return None, "none"
    match = _DATE_RE.match(text)
    if not match:
        raise ValueError(f"公告日期格式非法：{value}")
    year, month, day = match.groups()
    precision = "date"
    if _TIMESTAMP_RE.match(text):
        # 供应商即使返回时间戳，也不据此断言精确发布时刻；降级为日期级。
        precision = "date"
    return f"{year}-{month}-{day}", precision


def assert_snapshot_mode(membership_mode: str, in_date: str | None, out_date: str | None) -> None:
    if membership_mode == SNAPSHOT_ONLY:
        if in_date or out_date:
            raise StagingViolation("SNAPSHOT_ONLY 记录不得携带 in_date/out_date")
    elif membership_mode == INTERVAL:
        if not in_date:
            raise StagingViolation("INTERVAL 记录必须提供 in_date")
    else:
        raise StagingViolation(f"未知 membership_mode：{membership_mode}")


def assert_snapshot_not_from_current(snapshot_date: str, provider_asof: str | None) -> None:
    """禁止用当前快照回填历史截面。"""
    if not provider_asof:
        raise StagingViolation("缺少 provider_asof：不得用当前成分回填历史截面")
    if str(provider_asof)[:10] > str(snapshot_date)[:10]:
        raise StagingViolation(
            f"provider_asof={provider_asof} 晚于 snapshot_date={snapshot_date}：疑似当前快照回填"
        )


def make_mapping(
    *,
    provider: str,
    b1_sector_id: str,
    provider_sector_id: str | None,
    mapping_method: str,
    confidence: float,
) -> dict[str, Any]:
    """板块映射：模糊/未核对方法一律落为 pending，不能自动通过。"""
    if mapping_method in OFFICIAL_MAPPING_METHODS:
        review_status = "approved"
    elif mapping_method in CANDIDATE_MAPPING_METHODS:
        review_status = "pending"
    else:
        raise ValueError(f"未知 mapping_method：{mapping_method}")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence 必须在 0~1")
    return {
        "provider": provider,
        "b1_sector_id": b1_sector_id,
        "provider_sector_id": provider_sector_id,
        "mapping_method": mapping_method,
        "confidence": confidence,
        "review_status": review_status,
    }


def require_approved_mapping(row: dict[str, Any]) -> None:
    if row.get("review_status") != "approved":
        raise MappingNotApproved(
            f"{row.get('b1_sector_id')} -> {row.get('provider_sector_id')} 映射未批准"
        )
    if not row.get("provider_sector_id"):
        raise MappingNotApproved("已批准映射必须提供 provider_sector_id")


@runtime_checkable
class CorporateActionProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...

    def get_corporate_actions(self, request: CorporateActionRequest) -> ProviderResult: ...

    def get_adjustment_factors(self, request: FactorRequest) -> ProviderResult: ...


@runtime_checkable
class SectorMembershipProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...

    def get_sector_snapshot(self, request: SectorSnapshotRequest) -> ProviderResult: ...

    def get_sector_membership_events(self, request: MembershipEventRequest) -> ProviderResult: ...


class NotImplementedProvider:
    """未接入供应商占位：所有取数方法都抛错，不返回伪成功。"""

    def __init__(self, provider: str) -> None:
        if provider not in KNOWN_PROVIDERS:
            raise ValueError(f"未知供应商：{provider}")
        self.provider = provider

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.provider,
            datasets=(),
            history_start=None,
            pit_precision="none",
            supports_revision=False,
            license_scope="NOT_AUTHORIZED",
            notes="未接入：仅完成公开文档调查",
        )

    def get_corporate_actions(self, request: CorporateActionRequest) -> ProviderResult:
        raise ProviderNotImplemented(f"{self.provider} 公司行为适配器未接入")

    def get_adjustment_factors(self, request: FactorRequest) -> ProviderResult:
        raise ProviderNotImplemented(f"{self.provider} 复权因子适配器未接入")

    def get_sector_snapshot(self, request: SectorSnapshotRequest) -> ProviderResult:
        raise ProviderNotImplemented(f"{self.provider} 板块截面适配器未接入")

    def get_sector_membership_events(self, request: MembershipEventRequest) -> ProviderResult:
        raise ProviderNotImplemented(f"{self.provider} 成分变动适配器未接入")


class ProviderRegistry:
    """供应商注册表：未授权供应商在入口处硬拒绝。"""

    def __init__(self, allowed: frozenset[str] | set[str] | None = None) -> None:
        self._allowed = frozenset(allowed if allowed is not None else DEFAULT_AUTHORIZED)
        unknown = set(self._allowed) - set(KNOWN_PROVIDERS)
        if unknown:
            raise ValueError(f"未知供应商被列入授权：{sorted(unknown)}")
        self._adapters: dict[str, Any] = {}

    @property
    def allowed(self) -> frozenset[str]:
        return self._allowed

    def register(self, adapter: Any) -> None:
        caps = adapter.capabilities()
        self._adapters[caps.provider] = adapter

    def get(self, provider: str) -> Any:
        if provider not in KNOWN_PROVIDERS:
            raise ProviderNotAuthorized(f"未知供应商：{provider}")
        if provider not in self._allowed:
            raise ProviderNotAuthorized(f"{provider} 未获授权，拒绝调用")
        if provider not in self._adapters:
            raise ProviderNotAuthorized(f"{provider} 未注册适配器，拒绝调用")
        return self._adapters[provider]


SCHEMA_SQL = """
CREATE TABLE provider_manifest (
  batch_id TEXT, provider TEXT, transport TEXT, product TEXT,
  created_at TEXT, PRIMARY KEY (batch_id, provider)
);
CREATE TABLE provider_capability (
  batch_id TEXT, provider TEXT, datasets TEXT, history_start TEXT,
  pit_precision TEXT, supports_revision INTEGER, license_scope TEXT, notes TEXT,
  PRIMARY KEY (batch_id, provider)
);
CREATE TABLE raw_payload_manifest (
  record_id TEXT PRIMARY KEY,
  batch_id TEXT, provider TEXT, endpoint TEXT, payload_hash TEXT,
  bytes INTEGER, raw_json TEXT, ingested_at TEXT,
  UNIQUE (batch_id, provider, endpoint, payload_hash)
);
CREATE TABLE corporate_action_raw (
  record_id TEXT PRIMARY KEY,
  batch_id TEXT, provider TEXT, product TEXT, endpoint TEXT, field_code TEXT,
  code TEXT, event_type TEXT, announcement_date TEXT, announcement_precision TEXT,
  record_date TEXT, ex_date TEXT, pay_date TEXT, share_listing_date TEXT,
  cash_per_share_pretax REAL, cash_per_share_posttax REAL,
  stock_dividend_ratio REAL, transfer_ratio REAL,
  rights_ratio REAL, rights_price REAL, rights_shares REAL,
  plan_status TEXT, revision_id TEXT, available_at TEXT, ingested_at TEXT,
  payload_hash TEXT, quality_status TEXT, license_scope TEXT, source_record_id TEXT
);
CREATE TABLE adjustment_factor_raw (
  record_id TEXT PRIMARY KEY,
  batch_id TEXT, provider TEXT, product TEXT, endpoint TEXT, field_code TEXT,
  code TEXT, trade_date TEXT, factor REAL, factor_type TEXT, formula TEXT,
  provider_version TEXT, available_at TEXT, ingested_at TEXT,
  payload_hash TEXT, quality_status TEXT, license_scope TEXT, source_record_id TEXT
);
CREATE TABLE sector_master_raw (
  record_id TEXT PRIMARY KEY,
  batch_id TEXT, provider TEXT, provider_sector_id TEXT, provider_sector_name TEXT,
  taxonomy TEXT, level TEXT, available_at TEXT, ingested_at TEXT, payload_hash TEXT
);
CREATE TABLE sector_membership_snapshot (
  record_id TEXT PRIMARY KEY,
  batch_id TEXT, provider TEXT, provider_sector_id TEXT, provider_sector_name TEXT,
  taxonomy TEXT, snapshot_date TEXT, constituent_code TEXT, constituent_name TEXT,
  membership_mode TEXT, provider_asof TEXT, available_at TEXT, ingested_at TEXT,
  source_version TEXT, payload_hash TEXT, quality_status TEXT
);
CREATE TABLE sector_membership_event (
  record_id TEXT PRIMARY KEY,
  batch_id TEXT, provider TEXT, provider_sector_id TEXT, taxonomy TEXT,
  constituent_code TEXT, in_date TEXT, out_date TEXT,
  available_at TEXT, ingested_at TEXT, source_version TEXT, payload_hash TEXT
);
CREATE TABLE sector_id_crosswalk (
  batch_id TEXT, b1_sector_id TEXT, provider TEXT, provider_sector_id TEXT,
  provider_sector_name TEXT, taxonomy TEXT, mapping_method TEXT, confidence REAL,
  valid_from TEXT, valid_to TEXT, review_status TEXT,
  PRIMARY KEY (batch_id, b1_sector_id, provider)
);
CREATE TABLE ingestion_run (
  batch_id TEXT, provider TEXT, dataset TEXT, status TEXT,
  calls INTEGER, records INTEGER, started_at TEXT, finished_at TEXT, notes TEXT,
  PRIMARY KEY (batch_id, provider, dataset)
);
CREATE TABLE ingestion_error (
  batch_id TEXT, provider TEXT, dataset TEXT, error_code TEXT, detail TEXT, created_at TEXT
);
CREATE TABLE license_manifest (
  batch_id TEXT, provider TEXT, dataset TEXT, storage_license TEXT,
  website_usage_license TEXT, notes TEXT,
  PRIMARY KEY (batch_id, provider, dataset)
);
"""


class StagingBatch:
    """独立 staging 批次：目录唯一、只追加、不碰原始研究库。"""

    def __init__(self, root: Path, batch_id: str, *, now: str) -> None:
        if not re.fullmatch(r"[0-9A-Za-z_\-]+", batch_id):
            raise StagingViolation(f"batch_id 非法：{batch_id}")
        self.root = Path(root)
        self.batch_id = batch_id
        self.now = now
        self.dir = self.root / batch_id
        self.db_path = self.dir / "m65_multisource_staging.db"
        if self.db_path.name in PROTECTED_DB_NAMES:
            raise StagingViolation("staging 路径命中受保护数据库名")
        if self.dir.exists():
            raise StagingViolation(f"批次目录已存在，拒绝覆盖：{self.dir}")
        self.dir.mkdir(parents=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.executescript(SCHEMA_SQL)
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self.connection.commit()
            self.connection.close()
            self._closed = True

    def _insert(self, table: str, row: dict[str, Any]) -> None:
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        self.connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            tuple(row.values()),
        )

    def _record_id(self, *parts: str) -> str:
        return payload_hash([self.batch_id, *parts])[:32]

    def stage_raw(self, provider: str, endpoint: str, payload: Any) -> str:
        digest = payload_hash(payload)
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        record_id = self._record_id(provider, endpoint, digest)
        self._insert(
            "raw_payload_manifest",
            {
                "record_id": record_id,
                "batch_id": self.batch_id,
                "provider": provider,
                "endpoint": endpoint,
                "payload_hash": digest,
                "bytes": len(text.encode("utf-8")),
                "raw_json": text,
                "ingested_at": self.now,
            },
        )
        return record_id

    def stage_corporate_action(self, row: dict[str, Any]) -> str:
        announcement_date, precision = normalize_announcement(row.get("announcement_date"))
        record = {
            "batch_id": self.batch_id,
            "provider": row["provider"],
            "product": row.get("product"),
            "endpoint": row.get("endpoint"),
            "field_code": row.get("field_code"),
            "code": row["code"],
            "event_type": row.get("event_type"),
            "announcement_date": announcement_date,
            "announcement_precision": precision,
            "record_date": row.get("record_date"),
            "ex_date": row.get("ex_date"),
            "pay_date": row.get("pay_date"),
            "share_listing_date": row.get("share_listing_date"),
            "cash_per_share_pretax": row.get("cash_per_share_pretax"),
            "cash_per_share_posttax": row.get("cash_per_share_posttax"),
            "stock_dividend_ratio": row.get("stock_dividend_ratio"),
            "transfer_ratio": row.get("transfer_ratio"),
            "rights_ratio": row.get("rights_ratio"),
            "rights_price": row.get("rights_price"),
            "rights_shares": row.get("rights_shares"),
            "plan_status": row.get("plan_status"),
            "revision_id": row.get("revision_id"),
            "available_at": normalize_available_at(row.get("available_at")),
            "ingested_at": self.now,
            "payload_hash": payload_hash(row),
            "quality_status": row.get("quality_status", "OK"),
            "license_scope": row.get("license_scope", "NOT_AUTHORIZED"),
            "source_record_id": row.get("source_record_id"),
        }
        record_id = self._record_id("ca", row["provider"], row["code"], str(row.get("ex_date")))
        self._insert("corporate_action_raw", {"record_id": record_id, **record})
        return record_id

    def stage_factor(self, row: dict[str, Any]) -> str:
        record = {
            "batch_id": self.batch_id,
            "provider": row["provider"],
            "product": row.get("product"),
            "endpoint": row.get("endpoint"),
            "field_code": row.get("field_code"),
            "code": row["code"],
            "trade_date": row["trade_date"],
            "factor": row.get("factor"),
            "factor_type": row.get("factor_type", "UNKNOWN"),
            "formula": row.get("formula"),
            "provider_version": row.get("provider_version"),
            "available_at": normalize_available_at(row.get("available_at")),
            "ingested_at": self.now,
            "payload_hash": payload_hash(row),
            "quality_status": row.get("quality_status", "OK"),
            "license_scope": row.get("license_scope", "NOT_AUTHORIZED"),
            "source_record_id": row.get("source_record_id"),
        }
        record_id = self._record_id("factor", row["provider"], row["code"], row["trade_date"])
        self._insert("adjustment_factor_raw", {"record_id": record_id, **record})
        return record_id

    def stage_snapshot(self, row: dict[str, Any]) -> str:
        assert_snapshot_mode(
            row.get("membership_mode", SNAPSHOT_ONLY), row.get("in_date"), row.get("out_date")
        )
        assert_snapshot_not_from_current(row["snapshot_date"], row.get("provider_asof"))
        record = {
            "batch_id": self.batch_id,
            "provider": row["provider"],
            "provider_sector_id": row["provider_sector_id"],
            "provider_sector_name": row.get("provider_sector_name"),
            "taxonomy": row.get("taxonomy"),
            "snapshot_date": row["snapshot_date"],
            "constituent_code": row["constituent_code"],
            "constituent_name": row.get("constituent_name"),
            "membership_mode": row.get("membership_mode", SNAPSHOT_ONLY),
            "provider_asof": row.get("provider_asof"),
            "available_at": normalize_available_at(row.get("available_at")),
            "ingested_at": self.now,
            "source_version": row.get("source_version"),
            "payload_hash": payload_hash(row),
            "quality_status": row.get("quality_status", "OK"),
        }
        record_id = self._record_id(
            "snap", row["provider"], row["provider_sector_id"], row["snapshot_date"], row["constituent_code"]
        )
        self._insert("sector_membership_snapshot", {"record_id": record_id, **record})
        return record_id

    def stage_mapping(self, row: dict[str, Any]) -> None:
        self._insert(
            "sector_id_crosswalk",
            {
                "batch_id": self.batch_id,
                "b1_sector_id": row["b1_sector_id"],
                "provider": row["provider"],
                "provider_sector_id": row.get("provider_sector_id"),
                "provider_sector_name": row.get("provider_sector_name"),
                "taxonomy": row.get("taxonomy"),
                "mapping_method": row["mapping_method"],
                "confidence": row["confidence"],
                "valid_from": row.get("valid_from"),
                "valid_to": row.get("valid_to"),
                "review_status": row["review_status"],
            },
        )

    def stage_error(self, provider: str, dataset: str, error_code: str, detail: str) -> None:
        self._insert(
            "ingestion_error",
            {
                "batch_id": self.batch_id,
                "provider": provider,
                "dataset": dataset,
                "error_code": error_code,
                "detail": detail,
                "created_at": self.now,
            },
        )

    def counts(self) -> dict[str, int]:
        tables = (
            "raw_payload_manifest",
            "corporate_action_raw",
            "adjustment_factor_raw",
            "sector_membership_snapshot",
            "sector_id_crosswalk",
            "ingestion_error",
        )
        return {
            table: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }


def assert_not_research_db(path: Path) -> None:
    """任何写操作前调用：命中原始研究库立即失败。"""
    if Path(path).name in PROTECTED_DB_NAMES:
        raise StagingViolation(f"禁止写入原始研究库：{path}")
