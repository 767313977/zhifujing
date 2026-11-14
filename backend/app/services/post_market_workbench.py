"""S5-C：盘后主线研究工作台（只读聚合，不生成交易信号）。

把已经存在的六个只读能力聚合成一个页面所需的一次响应：

1. `sentiment_next_day`：D 情绪与 D+1 描述统计；
2. `limit_funnel`：1 进 2 / 2 进 3 / 3 进 4 / 4 进 5 与逐券四态；
3. `theme_intersection` + `sector_resonance`：已观测题材交集与连板分桶；
4. `data_quality`：来源状态与原因码；
5. `stock_research_context`：**只有显式传入 `stock` 时**才构建，避免逐券 N+1。

硬边界：
- 只读本地库；不发外部请求、不补采、不写库；
- 观察日必须是交易日；非交易日 `ValueError`（API 转 400）；
- `AND` 缺板块 → 共振 `UNVERIFIABLE`，不把「未知」当「确定没有交集」；
- D+1 未成熟 → `PENDING`，统计字段 null，不写 0；
- `null` 不自动映射 0；D+1 信息不回流到 D 的「当时已知」；
- 行数据只来自 `sector_resonance` 的分页结果，`sample_count` 不随分页变化。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import StockDaily, TradeCalendar
from app.services import data_quality as dq
from app.services import limit_funnel, sector_resonance, sentiment_next_day as snd
from app.services import stock_research_context as ctx
from app.services import theme_intersection as ti
from app.services.trading_days import previous_trade_day
from app.timeutil import today_cn

QUALITY_VERSION = "workbench-quality-v1"

VIEWS = ("overview", "ladder", "outcome", "quality")
MAX_SECTORS = 10
MAX_LIMIT = 100

NOTES = [
    "本页只是盘后研究汇总，不生成买入/卖出信号，也不进入 C0。",
    "题材成员快照没有完成证明；所有比率/交集都是「已观测样本内」口径。",
    "D+1 表现是事后描述统计，不是可交易收益；未成熟时一律 null。",
    "个股研究依据只在显式选中个股时加载，不对全表逐券调用。",
]


def _resolve_day(session: Session, as_of: date | None) -> date:
    calendar = list(
        session.scalars(select(TradeCalendar.trade_date).order_by(TradeCalendar.trade_date))
    )
    if not calendar:
        raise ValueError("交易日历为空，无法确定观察日")
    if as_of is None:
        candidates = [day for day in calendar if day <= today_cn()]
        if not candidates:
            raise ValueError("没有可用的观察日")
        return candidates[-1]
    if as_of not in calendar:
        raise ValueError(f"{as_of} 不是交易日，不能作为观察日")
    return as_of


def _sentiment_block(session: Session, day: date) -> dict[str, Any]:
    data = snd.build_next_day(session, days=1, end_date=day)
    row = next(
        (item for item in data.get("series", []) if item.get("sample_date") == day),
        None,
    )
    if row is None:
        return {
            "status": "PENDING",
            "as_of": day,
            "summary": None,
            "reasons": ["SENTIMENT_SAMPLE_PENDING"],
        }
    summary = {
        "limit_up_count": row.get("limit_up_count"),
        "broken_rate": row.get("broken_rate"),
        "max_consecutive": row.get("max_consecutive"),
        "advance_ratio": row.get("advance_ratio"),
    }
    return {
        "status": row.get("data_quality_status") or "UNVERIFIABLE",
        "as_of": day,
        "summary": summary,
        "outcome_date": row.get("outcome_date"),
        "outcome": {
            "sample_count": row.get("sample_count"),
            "valid_count": row.get("valid_count"),
            "missing_count": row.get("missing_count"),
            "avg_pct": row.get("avg_pct"),
            "median_pct": row.get("median_pct"),
            "up_ratio": row.get("up_ratio"),
            "limit_up_again_ratio": row.get("limit_up_again_ratio"),
            "label": "D+1 描述统计（事后，不可交易）",
        },
        "reasons": list(row.get("data_quality_reasons") or []),
    }


def _promotion_block(session: Session, day: date) -> dict[str, Any]:
    prev = previous_trade_day(day, session)
    if prev is None:
        return {
            "status": limit_funnel.NOT_COLLECTED,
            "prev_date": None,
            "levels": [],
            "reasons": ["NO_PREVIOUS_TRADE_DAY"],
        }
    inputs = limit_funnel.load_funnel_inputs(session, prev, day)
    levels, quality = limit_funnel.build_promotion_funnel(inputs, [1, 2, 3, 4])
    return {
        "status": quality.status,
        "prev_date": prev,
        "levels": [
            {
                "level": row["level"],
                "label": row["label"],
                "base": row["base"],
                "promoted": row["promoted"],
                "broken": row["broken"],
                "failed": row["failed"],
                "unverifiable": row["unverifiable"],
                "valid_base": row["valid_base"],
                "legacy_rate": row["legacy_rate"],
                "verified_rate": row["verified_rate"],
            }
            for row in levels
        ],
        "reasons": list(quality.reasons),
    }


def _quality_block(session: Session, day: date) -> dict[str, Any]:
    overview = dq.overview(session, target_date=day, days=30)
    sources = []
    warnings: list[str] = []
    for item in overview["items"]:
        status = item["status"]
        sources.append(
            {
                "source": item["source"],
                "status": status,
                "combined_view_status": item.get("combined_view_status", status),
                "completion_proof": item["completion_proof"],
                "reason_codes": list(item["reason_codes"]),
            }
        )
        if status not in (dq.STATUS_VERIFIED, dq.STATUS_NOT_TRADE_DAY):
            warnings.extend(f"{item['source']}:{code}" for code in item["reason_codes"])
    return {
        "version": dq.QUALITY_VERSION,
        "sources": sources,
        "warnings": list(dict.fromkeys(warnings)),
    }


def _resonance_block(
    session: Session,
    *,
    day: date,
    sector_codes: list[str],
    mode: str,
    market: str,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    if not sector_codes:
        return {
            "status": "NOT_SELECTED",
            "coverage": None,
            "state_counts": {},
            "outcome_status": "NOT_SELECTED",
            "outcome": None,
            "pagination": {"total": 0, "shown": 0, "has_more": False},
            "rows": [],
            "warnings": ["NO_SECTORS_SELECTED"],
        }
    data = sector_resonance.build(
        session,
        as_of=day,
        sector_codes=sector_codes,
        mode=mode,
        market=market,
        limit=limit,
        offset=offset,
    )
    return {
        "status": data["quality"]["status"],
        "reasons": data["quality"]["reasons"],
        "coverage": data["coverage"],
        "state_counts": data["state_counts"],
        "outcome_status": data["outcome_status"],
        "outcome": data["outcome"],
        "outcome_reason_codes": data["outcome_reason_codes"],
        "by_state": data["by_state"],
        "pagination": {
            "total": data["total"],
            "shown": data["shown"],
            "has_more": data["has_more"],
        },
        "rows": data["rows"],
        "warnings": data["warnings"],
        "pit": data["pit"],
        "member_snapshot_date": data["member_snapshot_date"],
        "outcome_date": data["outcome_date"],
    }


def build(
    session: Session,
    *,
    as_of: date | None = None,
    sector_codes: list[str] | None = None,
    mode: str = "AND",
    market: str = "all",
    view: str = "overview",
    limit: int = 50,
    offset: int = 0,
    stock: str | None = None,
) -> dict[str, Any]:
    """聚合一张盘后研究工作台响应（只读）。参数非法时 `ValueError`。"""
    mode = (mode or "AND").upper()
    market = (market or "all").lower()
    view = (view or "overview").lower()
    if mode not in ti.MODES:
        raise ValueError(f"未知模式：{mode}")
    if market not in ti.MARKETS:
        raise ValueError(f"未知市场：{market}")
    if view not in VIEWS:
        raise ValueError(f"未知研究视图：{view}")
    if limit < 1 or limit > min(MAX_LIMIT, ti.MAX_LIMIT):
        raise ValueError(f"limit 必须在 1~{min(MAX_LIMIT, ti.MAX_LIMIT)} 之间")
    if offset < 0 or offset > 1_000_000:
        raise ValueError("offset 非法")
    codes = ti._normalize_codes(sector_codes) if sector_codes else []
    if len(codes) > MAX_SECTORS:
        raise ValueError(f"板块最多 {MAX_SECTORS} 个")

    day = _resolve_day(session, as_of)
    data_as_of = session.scalar(select(func.max(StockDaily.trade_date)))
    sentiment = _sentiment_block(session, day)
    promotion = _promotion_block(session, day)
    resonance = _resonance_block(
        session,
        day=day,
        sector_codes=codes,
        mode=mode,
        market=market,
        limit=limit,
        offset=offset,
    )
    quality = _quality_block(session, day)

    stock_research = None
    if stock:
        stock_research = ctx.build(session, code=stock, as_of=day)

    warnings: list[str] = []
    warnings.extend(resonance.get("warnings") or [])
    if resonance["status"] == "NOT_SELECTED":
        warnings.append("NO_SECTORS_SELECTED")
    elif resonance.get("coverage") and not resonance["coverage"].get(
        "member_completion_proof"
    ):
        warnings.append("OBSERVED_SAMPLE_ONLY")
    if resonance["outcome_status"] == "PENDING":
        warnings.append("OUTCOME_PENDING")
    if promotion["status"] != "VERIFIED":
        warnings.extend(f"promotion:{code}" for code in promotion["reasons"])

    return {
        "as_of": day,
        "data_as_of": data_as_of,
        "quality_version": QUALITY_VERSION,
        "scope": {
            "sector_codes": codes,
            "mode": mode,
            "market": market,
            "view": view,
        },
        "sentiment": sentiment,
        "promotion": promotion,
        "resonance": {
            "status": resonance["status"],
            "coverage": resonance.get("coverage"),
            "state_counts": resonance.get("state_counts") or {},
            "member_snapshot_date": resonance.get("member_snapshot_date"),
            "pit": resonance.get("pit"),
            "reasons": resonance.get("reasons") or [],
            "warnings": resonance.get("warnings") or [],
        },
        "outcome": {
            "status": resonance["outcome_status"],
            "outcome_date": resonance.get("outcome_date"),
            "stats": resonance.get("outcome"),
            "by_state": resonance.get("by_state") or {},
            "reasons": resonance.get("outcome_reason_codes") or [],
        },
        "quality": quality,
        "pagination": resonance["pagination"],
        "rows": resonance["rows"],
        "stock_research": stock_research,
        "warnings": list(dict.fromkeys(warnings)),
        "notes": NOTES,
    }
