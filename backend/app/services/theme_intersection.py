"""题材 AND / OR 交叉检索 v2（S3-A）。

数据边界（先看清再读数）：

- 只用本地 `sector_member` 快照（`trade_date + sector_code + code`），**不发外部
  请求、不补采、不写库**；
- `sector_member` 没有成员级 / 板块级完成日志。S3 起把两件事彻底分开：
  - **观察覆盖**：当天在该 taxonomy 下观察到多少个板块（`observed_sector_count`）；
  - **完成证明**：是否有可靠证据证明成员写全（`has_completion_proof`）。
  数量达标 **不等于** 成员完整，因此当前数据源一律 `has_completion_proof=false`，
  结果最多 `PARTIAL`，绝不返回 `VERIFIED`。
- `stock_concept` 只收录**涨停股**，不能当全市场成员表用，本服务不拿它做交集；
- 同一个板块代码在不同 taxonomy（精选 / 行业）下可能重名，返回里每个板块都带
  `taxonomy`，不做「同名合并」；
- AND 缺数据时**不得**把「未知」当成「确定没有交集」：直接返回 `UNVERIFIABLE`
  与空结果，并在 `warnings` 里点名是哪几个板块没覆盖；
- PIT：`SectorMember.fetched_at` 是**本系统写入时间**，只能证明「我们何时拿到」，
  不能证明上游在观察日盘前已发布。同一天取到的快照一律 `UNKNOWN`，不猜 true。

排序稳定（代码升序）+ offset 分页；所有查询都是批量 IN，分页后再查涨停标记。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import LimitPool, SectorBasic, SectorMember

MAX_SECTORS = 10
MAX_LIMIT = 500
_CODE = re.compile(r"^[0-9A-Za-z]{2,16}$")

MODES = ("AND", "OR")
MARKETS = ("all", "sh", "sz", "bj")

#: 质量模型版本。字段语义变化必须升版本，避免旧前端把「板块数达标」当成完成证明。
QUALITY_VERSION = "theme-quality-v2"

STATUS_VERIFIED = "VERIFIED"  # 当前数据源不可达；保留常量供未来完成协议使用
STATUS_PARTIAL = "PARTIAL"
STATUS_NOT_COLLECTED = "NOT_COLLECTED"
STATUS_UNVERIFIABLE = "UNVERIFIABLE"

COVERAGE_COMPLETE_OBSERVED = "COMPLETE_OBSERVED_COUNT"
COVERAGE_PARTIAL_OBSERVED = "PARTIAL_OBSERVED_COUNT"
COVERAGE_UNKNOWN = "UNKNOWN"

#: `sector_member` 没有「当天全量成员已写完」的采集日志。这个提示必须一直带着，
#: 否则页面很容易把「快照里没有」读成「市场里没有」。
NO_COMPLETION_LOG = "NO_MEMBERS_COMPLETION_LOG"

WARNING_TEXT = {
    NO_COMPLETION_LOG: "sector_member 没有成员级完成日志，结果按快照覆盖解释。",
    "SNAPSHOT_SECTOR_COVERAGE_LOW": "当天板块成员快照未覆盖全部基础板块。",
    "SELECTED_SECTOR_NOT_COVERED": "所选板块在该日期没有成员快照。",
    "AND_UNVERIFIABLE": "AND 结果不可信：缺失板块不能被当成「确定没有交集」。",
    "TAXONOMY_MIXED": "所选板块跨 taxonomy，同名板块不代表同一口径。",
    "MARKET_FILTER_APPLIED": "已按市场过滤，未匹配市场的证券不在结果内。",
    "STOCK_CONCEPT_NOT_USED": "stock_concept 只覆盖涨停股，未用于全市场交集。",
    "OBSERVED_INTERSECTION_ONLY": "结果只是已观测交集，不等于市场真实全量交集。",
    "THEME_PIT_UNKNOWN": "题材快照无法证明在观察日盘前已知，不能作为当时交易信号。",
}


def _market_of(code: str) -> str:
    if code.startswith(("60", "68", "900")):
        return "sh"
    if code.startswith(("00", "30", "200")):
        return "sz"
    if code.startswith(("4", "8", "92")):
        return "bj"
    return "other"


def _normalize_codes(raw: list[str]) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for item in raw:
        value = (item or "").strip()
        if not value or value in seen:
            continue
        if not _CODE.match(value):
            raise ValueError(f"非法板块代码：{value}")
        seen.add(value)
        codes.append(value)
    if not codes:
        raise ValueError("至少需要 1 个板块代码")
    if len(codes) > MAX_SECTORS:
        raise ValueError(f"最多同时选择 {MAX_SECTORS} 个板块")
    return codes


def _chunked(values: list[str], size: int = 800) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _known_by(requested: date | None, fetched_at: datetime | None) -> bool | None:
    """能否证明「观察日盘前已知」。

    - 未指定观察日（看最新）→ 不做历史声明，None；
    - 快照在观察日之前取到 → True；
    - 快照在观察日之后才取到 → False；
    - 同一天取到 → 无法区分盘前/盘后，None（不猜）。
    """
    if requested is None or fetched_at is None:
        return None
    fetched_date = fetched_at.date() if isinstance(fetched_at, datetime) else None
    if fetched_date is None:
        return None
    if fetched_date < requested:
        return True
    if fetched_date > requested:
        return False
    return None


def search(
    session: Session,
    *,
    sector_codes: list[str],
    mode: str = "AND",
    market: str = "all",
    as_of: date | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """执行一次只读题材交叉检索。参数非法时抛 `ValueError`（API 转 400）。"""
    mode = (mode or "AND").upper()
    market = (market or "all").lower()
    if mode not in MODES:
        raise ValueError(f"未知模式：{mode}")
    if market not in MARKETS:
        raise ValueError(f"未知市场：{market}")
    if limit < 1 or limit > MAX_LIMIT:
        raise ValueError(f"limit 必须在 1~{MAX_LIMIT} 之间")
    if offset < 0 or offset > 1_000_000:
        raise ValueError("offset 非法")
    codes = _normalize_codes(sector_codes)

    basics = {
        row.code: row
        for row in session.scalars(select(SectorBasic).where(SectorBasic.code.in_(codes)))
    }
    unknown = [code for code in codes if code not in basics]
    if unknown:
        raise ValueError(f"未知板块代码：{'、'.join(unknown)}")

    # 解析快照日：显式 as_of 时**只向过去找**，绝不拿未来快照顶替
    stmt = select(func.max(SectorMember.trade_date)).where(
        SectorMember.sector_code.in_(codes)
    )
    if as_of is not None:
        stmt = stmt.where(SectorMember.trade_date <= as_of)
    resolved = session.scalar(stmt)

    selected = [
        {
            "code": code,
            "name": basics[code].name,
            "taxonomy": basics[code].taxonomy,
            "member_count": 0,
            "has_data": False,
        }
        for code in codes
    ]
    taxonomies = sorted({item["taxonomy"] for item in selected})
    taxonomy = taxonomies[0] if len(taxonomies) == 1 else "mixed"
    base = {
        "requested_as_of": as_of,
        "resolved_as_of": resolved,
        "source_snapshot_date": resolved,
        "fetched_at": None,
        "is_exact_date": bool(as_of is None or resolved == as_of),
        "known_by_observation_time": None,
        "historical_signal_eligible": False,
        "snapshot_age_days": None,
        "quality_version": QUALITY_VERSION,
        "taxonomy": taxonomy,
        "selected_sectors": selected,
        "mode": mode,
        "market": market,
        "total": 0,
        "shown": 0,
        "has_more": False,
        "offset": offset,
        "limit": limit,
        "coverage": {
            "observed_sector_count": 0,
            "expected_sector_count": 0,
            # 旧字段：S3 起与 observed_sector_count 同义（同 taxonomy、去重键为
            # (taxonomy, sector_code)），仅为兼容保留
            "sector_count": 0,
            "expected_sectors": 0,
            "sample_rows": 0,
            "distinct_stocks": 0,
            "coverage_taxonomies": taxonomies,
            "selected_sector_count": len(codes),
            "selected_sector_observed": 0,
            "has_completion_proof": False,
            # 旧字段：S3 起严格映射为完成证明，不再用「板块数达标」冒充
            "has_completeness_evidence": False,
            "coverage_status": COVERAGE_UNKNOWN,
            "coverage_unknown": True,
            "note": WARNING_TEXT[NO_COMPLETION_LOG],
        },
        "quality": {
            "status": STATUS_NOT_COLLECTED,
            "version": QUALITY_VERSION,
            "reasons": [],
        },
        "warnings": [NO_COMPLETION_LOG, "STOCK_CONCEPT_NOT_USED"],
        "items": [],
    }
    if resolved is None:
        base["quality"] = {
            "status": STATUS_NOT_COLLECTED,
            "version": QUALITY_VERSION,
            "reasons": ["NO_SNAPSHOT_FOR_SELECTED_SECTORS"],
        }
        return base

    rows = session.execute(
        select(
            SectorMember.sector_code,
            SectorMember.code,
            SectorMember.name,
            SectorMember.fetched_at,
        ).where(
            SectorMember.trade_date == resolved,
            SectorMember.sector_code.in_(codes),
        )
    ).all()

    counts: dict[str, int] = {}
    by_code: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    fetched_at: datetime | None = None
    for sector_code, code, name, stamp in rows:
        counts[sector_code] = counts.get(sector_code, 0) + 1
        by_code.setdefault(code, set()).add(sector_code)
        if name and code not in names:
            names[code] = name
        if stamp is not None and (fetched_at is None or stamp > fetched_at):
            fetched_at = stamp

    for item in selected:
        item["member_count"] = counts.get(item["code"], 0)
        item["has_data"] = item["member_count"] > 0

    warnings = [NO_COMPLETION_LOG, "STOCK_CONCEPT_NOT_USED"]
    reasons: list[str] = []
    missing = [item["code"] for item in selected if not item["has_data"]]
    if missing:
        warnings.append("SELECTED_SECTOR_NOT_COVERED")
        reasons.append("SELECTED_SECTOR_NOT_COVERED")
    if len(taxonomies) > 1:
        warnings.append("TAXONOMY_MIXED")
        reasons.append("TAXONOMY_MIXED")

    if mode == "AND" and missing:
        # 不知道不等于「没有交集」：拒绝给出确定性的空结果
        warnings.append("AND_UNVERIFIABLE")
        status = STATUS_UNVERIFIABLE
        reasons.append("AND_UNVERIFIABLE")
        matched: list[str] = []
    else:
        if mode == "AND":
            matched = [
                code
                for code, hit in by_code.items()
                if all(sector in hit for sector in codes)
            ]
        else:
            matched = list(by_code)
        # 没有完成证明时，任何有结果的查询都只是「已观测交集」
        status = STATUS_PARTIAL
        reasons.append("OBSERVED_INTERSECTION_ONLY")
        warnings.append("OBSERVED_INTERSECTION_ONLY")

    if market != "all":
        matched = [code for code in matched if _market_of(code) == market]
        warnings.append("MARKET_FILTER_APPLIED")

    matched.sort()
    total = len(matched)
    page_codes = matched[offset : offset + limit]

    consecutive: dict[str, int | None] = {}
    if page_codes:
        for chunk in _chunked(page_codes):
            for code, value in session.execute(
                select(LimitPool.code, LimitPool.consecutive).where(
                    LimitPool.trade_date == resolved,
                    LimitPool.pool_type == "up",
                    LimitPool.code.in_(chunk),
                )
            ).all():
                consecutive[code] = value

    sector_names = {item["code"]: item["name"] for item in selected}
    items = [
        {
            "code": code,
            "name": names.get(code),
            "match_count": len(by_code.get(code, ())),
            "sectors": [
                {"code": sector_code, "name": sector_names.get(sector_code)}
                for sector_code in codes
                if sector_code in by_code.get(code, ())
            ],
            "trade_date": resolved,
            "limit_up_consecutive": consecutive.get(code),
        }
        for code in page_codes
    ]

    # 覆盖：只用**与所选板块相同的 taxonomy**，去重键 (taxonomy, sector_code)，
    # 绝不用跨 taxonomy 的 global distinct 数量来证明完整性。
    observed_keys = session.execute(
        select(SectorBasic.taxonomy, SectorMember.sector_code)
        .join(SectorBasic, SectorBasic.code == SectorMember.sector_code)
        .where(
            SectorMember.trade_date == resolved,
            SectorBasic.taxonomy.in_(taxonomies),
        )
        .distinct()
    ).all()
    observed = len({(row[0], row[1]) for row in observed_keys})
    expected = session.scalar(
        select(func.count())
        .select_from(SectorBasic)
        .where(SectorBasic.taxonomy.in_(taxonomies))
    )
    expected = int(expected or 0)
    distinct_stocks = session.scalar(
        select(func.count(func.distinct(SectorMember.code)))
        .select_from(SectorMember)
        .join(SectorBasic, SectorBasic.code == SectorMember.sector_code)
        .where(
            SectorMember.trade_date == resolved,
            SectorBasic.taxonomy.in_(taxonomies),
        )
    )
    distinct_stocks = int(distinct_stocks or 0)
    selected_observed = sum(1 for item in selected if item["has_data"])

    if expected <= 0 or not taxonomies:
        coverage_status = COVERAGE_UNKNOWN
        coverage_unknown = True
        reasons.append("COVERAGE_DEFINITION_UNKNOWN")
    else:
        coverage_unknown = False
        coverage_status = (
            COVERAGE_COMPLETE_OBSERVED
            if observed >= expected
            else COVERAGE_PARTIAL_OBSERVED
        )
        if observed < expected:
            warnings.append("SNAPSHOT_SECTOR_COVERAGE_LOW")
            reasons.append("SNAPSHOT_SECTOR_COVERAGE_LOW")

    # 当前数据源没有成员级完成证明：数量达标也不能升级
    has_completion_proof = False
    reasons.append(NO_COMPLETION_LOG)
    status = STATUS_UNVERIFIABLE if status == STATUS_UNVERIFIABLE else STATUS_PARTIAL

    known_by = _known_by(as_of, fetched_at)
    if as_of is not None and known_by is not True:
        warnings.append("THEME_PIT_UNKNOWN")
        reasons.append("THEME_PIT_UNKNOWN")

    note = (
        f"已观测 {observed}/{expected} 个同 taxonomy 板块；"
        "成员级完成日志缺失，不能证明成员完整。"
    )

    base.update(
        {
            "fetched_at": fetched_at,
            "known_by_observation_time": known_by,
            "historical_signal_eligible": False,
            "snapshot_age_days": (
                (as_of - resolved).days if as_of is not None and resolved is not None else None
            ),
            "total": total,
            "shown": len(items),
            "has_more": offset + len(items) < total,
            "coverage": {
                "observed_sector_count": observed,
                "expected_sector_count": expected,
                "sector_count": observed,
                "expected_sectors": expected,
                "sample_rows": len(rows),
                "distinct_stocks": distinct_stocks,
                "coverage_taxonomies": taxonomies,
                "selected_sector_count": len(codes),
                "selected_sector_observed": selected_observed,
                "has_completion_proof": has_completion_proof,
                "has_completeness_evidence": has_completion_proof,
                "coverage_status": coverage_status,
                "coverage_unknown": coverage_unknown,
                "note": note,
            },
            "quality": {
                "status": status,
                "version": QUALITY_VERSION,
                "reasons": _dedupe(reasons),
            },
            "warnings": _dedupe(warnings),
            "items": items,
        }
    )
    return base


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    for item in items:
        if item and item not in result:
            result.append(item)
    return result


def warning_text(code: str) -> str:
    return WARNING_TEXT.get(code, code)
