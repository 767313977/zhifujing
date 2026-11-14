"""盘后「板块主线共振」研究工作台（S4-C）—— 只读，不预测、不选股。

把三块已经存在的数据接在一起：

1. `sector_member` 快照（成员覆盖，**没有完成证明**）；
2. 同日 `limit_pool` 连板梯队（首板 / 二板 / 高位 / 炸板）；
3. `stock_daily` 的 D+1 描述性表现（pooled 均值 / 中位数 / 再涨停比例）。

硬边界：
- 只读本地库；不发外部请求、不补采、不写库；
- `as_of` 必须是交易日；快照日 `member_snapshot_date` **只向过去找**，不拿未来快照顶替；
- 成员快照没有完成证明 → 所有比率都只是「已观测样本内」的比率，命名里不出现
  「全板块涨停率」，`completion_proof=false`；
- AND 缺板块 → `UNVERIFIABLE`，不把「未知」当「确定没有交集」；
- 同一股票同时出现在涨停与炸板池 → `POOL_CONFLICT`，不双计、不按优先级择一；
- D+1 行情未到齐 → `PENDING`，统计字段 null，不写 0%；
- D+1 涨停池不可信 → 再涨停比例 null，不拿 `pct_chg >= 9.9` 猜。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    CollectLog,
    LimitPool,
    MarketSentiment,
    SectorBasic,
    SectorMember,
    StockDaily,
    TradeCalendar,
)
from app.services import sentiment_next_day as snd
from app.services import theme_intersection as ti
from app.timeutil import today_cn

QUALITY_VERSION = "sector-resonance-v1"
AGGREGATION_VERSION = snd.AGGREGATION_VERSION

STATUS_VERIFIED = "VERIFIED"
STATUS_PARTIAL = "PARTIAL"
STATUS_PENDING = "PENDING"
STATUS_UNVERIFIABLE = "UNVERIFIABLE"
STATUS_NOT_COLLECTED = "NOT_COLLECTED"

STATE_FIRST = "first_board"
STATE_SECOND = "second_board"
STATE_THIRD_PLUS = "third_plus"
STATE_BROKEN = "broken"
STATE_CONFLICT = "conflict"
STATE_UNKNOWN = "unknown"

STATE_LABELS = {
    STATE_FIRST: "首板",
    STATE_SECOND: "二板",
    STATE_THIRD_PLUS: "三板及以上",
    STATE_BROKEN: "炸板",
    STATE_CONFLICT: "涨停/炸板冲突",
    STATE_UNKNOWN: "连板高度未知",
}

REASON_TEXT = {
    "NO_MEMBERS_SNAPSHOT": "所选板块在该日期没有成员快照",
    "SELECTED_SECTOR_NOT_COVERED": "部分所选板块没有成员快照",
    "AND_UNVERIFIABLE": "AND 结果不可信：缺失板块不能被当成「确定没有交集」",
    "TAXONOMY_MIXED": "所选板块跨 taxonomy，同名板块不代表同一口径",
    "NO_MEMBERS_COMPLETION_LOG": "成员快照没有完成证明，比率仅限已观测样本",
    "OBSERVED_ONLY": "结果只是已观测交集/并集，不等于市场真实全量",
    "POOL_UP_UNVERIFIED": "D 日涨停池不可信，连板分桶与涨停数不可用",
    "POOL_BROKEN_UNVERIFIED": "D 日炸板池不可信，炸板分桶不可用",
    "POOL_CONFLICT": "同一股票同时出现在涨停与炸板池，单独列出且不双计",
    "OUTCOME_PENDING": "D+1 行情尚未到齐",
    "OUTCOME_PARTIAL": "D+1 部分股票缺行情",
    "OUTCOME_LIMIT_POOL_UNVERIFIED": "D+1 涨停池不可信，再涨停比例不可计算",
    "NO_NEXT_TRADE_DAY": "没有下一交易日",
    "MARKET_FILTER_APPLIED": "已按市场过滤",
}

NOTES = [
    "成员数来自 sector_member 快照，没有完成证明；所有比率都是「已观测样本内」口径。",
    "D+1 表现是事后描述统计，不是可交易收益，也不进入 C0 或任何正式信号。",
    "快照若在观察日之后才取到，仍可用于复盘展示，但不能当作当时已知。",
]


def _market_of(code: str) -> str:
    if code.startswith(("60", "68", "900")):
        return "sh"
    if code.startswith(("00", "30", "200")):
        return "sz"
    if code.startswith(("4", "8", "92")):
        return "bj"
    return "other"


def _snapshot_known_by(fetched_at, as_of: date) -> str:
    if fetched_at is None or not hasattr(fetched_at, "date"):
        return "UNKNOWN"
    day = fetched_at.date()
    if day < as_of:
        return "PRIOR_DAY"
    if day > as_of:
        return "LATER"
    return "SAME_DAY_UNKNOWN"


def _latest_log(session: Session, day: date, task: str) -> CollectLog | None:
    return session.scalars(
        select(CollectLog)
        .where(CollectLog.trade_date == day, CollectLog.task == task)
        .order_by(CollectLog.id.desc())
        .limit(1)
    ).first()


def _pool_trust(
    session: Session, day: date, task: str, pool_type: str, *, allow_empty: bool
) -> tuple[bool, str | None]:
    log = _latest_log(session, day, task)
    rows = session.scalar(
        select(func.count())
        .select_from(LimitPool)
        .where(LimitPool.trade_date == day, LimitPool.pool_type == pool_type)
    ) or 0
    if log is None:
        return False, "POOL_LOG_MISSING" if rows else "POOL_NOT_COLLECTED"
    status = (log.status or "").lower()
    if status != "ok":
        return False, f"POOL_STATUS_{status.upper()}"
    if rows == 0 and not allow_empty:
        return False, "UP_POOL_EMPTY_WITH_OK_LOG"
    return True, None


def _quotes(session: Session, day: date, codes: list[str]) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}
    rows = session.execute(
        select(
            StockDaily.code,
            StockDaily.pct_chg,
            StockDaily.open,
            StockDaily.close,
        ).where(StockDaily.trade_date == day, StockDaily.code.in_(codes))
    ).all()
    return {
        code: {"pct_chg": pct, "open": open_, "close": close, "prev_close": None}
        for code, pct, open_, close in rows
    }


def _empty_stats() -> dict[str, Any]:
    return {
        "sample_count": 0,
        "valid_count": 0,
        "missing_count": 0,
        "avg_pct": None,
        "median_pct": None,
        "outcome_up_count": 0,
        "outcome_down_count": 0,
        "outcome_flat_count": 0,
        "up_ratio": None,
        "limit_up_again_count": None,
        "limit_up_again_ratio": None,
        "not_limit_up_count": None,
        "unverifiable_count": 0,
        "avg_open_pct": None,
        "avg_close_vs_open": None,
    }


def _stats_block(
    codes: list[str],
    quotes: dict[str, dict[str, Any]],
    *,
    limit_up_codes: set[str] | None,
) -> dict[str, Any]:
    if not codes:
        return _empty_stats()
    stats, _ = snd._stats(codes, quotes, limit_up_codes=limit_up_codes)
    return stats


def build(
    session: Session,
    *,
    as_of: date | None = None,
    sector_codes: list[str],
    mode: str = "AND",
    market: str = "all",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """单日「板块 × 连板梯队 × D+1」只读研究结果。参数非法时 `ValueError`。"""
    mode = (mode or "AND").upper()
    market = (market or "all").lower()
    if mode not in ti.MODES:
        raise ValueError(f"未知模式：{mode}")
    if market not in ti.MARKETS:
        raise ValueError(f"未知市场：{market}")
    if limit < 1 or limit > ti.MAX_LIMIT:
        raise ValueError(f"limit 必须在 1~{ti.MAX_LIMIT} 之间")
    if offset < 0 or offset > 1_000_000:
        raise ValueError("offset 非法")
    codes = ti._normalize_codes(sector_codes)

    basics = {
        row.code: row
        for row in session.scalars(select(SectorBasic).where(SectorBasic.code.in_(codes)))
    }
    unknown = [code for code in codes if code not in basics]
    if unknown:
        raise ValueError(f"未知板块代码：{'、'.join(unknown)}")

    calendar = list(
        session.scalars(select(TradeCalendar.trade_date).order_by(TradeCalendar.trade_date))
    )
    if not calendar:
        raise ValueError("交易日历为空，无法确定观察日")
    if as_of is None:
        candidates = [day for day in calendar if day <= today_cn()]
        if not candidates:
            raise ValueError("没有可用的观察日")
        resolved = candidates[-1]
    else:
        if as_of not in calendar:
            raise ValueError(f"{as_of} 不是交易日，不能作为观察日")
        resolved = as_of
    position = {day: index for index, day in enumerate(calendar)}
    following = (
        calendar[position[resolved] + 1]
        if position[resolved] + 1 < len(calendar)
        else None
    )

    taxonomies = sorted({basics[code].taxonomy for code in codes})
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

    snapshot_date = session.scalar(
        select(func.max(SectorMember.trade_date)).where(
            SectorMember.sector_code.in_(codes),
            SectorMember.trade_date <= resolved,
        )
    )
    base: dict[str, Any] = {
        "as_of": resolved,
        "member_snapshot_date": snapshot_date,
        "outcome_date": following,
        "requested_sector_codes": codes,
        "mode": mode,
        "market": market,
        "quality_version": QUALITY_VERSION,
        "aggregation_version": AGGREGATION_VERSION,
        "taxonomy": taxonomies[0] if len(taxonomies) == 1 else "mixed",
        "selected_sectors": selected,
        "coverage": {
            "member_observed_rows": 0,
            "member_unique_count": 0,
            "member_completion_proof": False,
            "observed_sectors": 0,
            "expected_sectors": len(codes),
            "observed_limit_up_count": None,
            "observed_limit_up_ratio": None,
            "ratio_scope": "OBSERVED_SAMPLE_ONLY",
            "note": REASON_TEXT["NO_MEMBERS_COMPLETION_LOG"],
        },
        "pit": {
            "historical_signal_eligible": False,
            "member_snapshot_known_by": None,
            "known_by_observation_time": None,
            "later_snapshot_for_review": None,
        },
        "market_sentiment": None,
        "state_counts": {key: 0 for key in STATE_LABELS},
        "outcome": _empty_stats(),
        "outcome_status": STATUS_PENDING,
        "outcome_reason_codes": [],
        "by_state": {},
        "total": 0,
        "shown": 0,
        "has_more": False,
        "offset": offset,
        "limit": limit,
        "quality": {"status": STATUS_NOT_COLLECTED, "reasons": []},
        "warnings": [
            "NO_MEMBERS_COMPLETION_LOG",
            "OBSERVED_ONLY",
        ],
        "notes": NOTES,
        "rows": [],
    }
    if snapshot_date is None:
        base["quality"] = {
            "status": STATUS_NOT_COLLECTED,
            "reasons": ["NO_MEMBERS_SNAPSHOT"],
        }
        base["warnings"].append("NO_MEMBERS_SNAPSHOT")
        return base

    member_rows = session.execute(
        select(
            SectorMember.code,
            SectorMember.name,
            SectorMember.sector_code,
            SectorMember.fetched_at,
        ).where(
            SectorMember.trade_date == snapshot_date,
            SectorMember.sector_code.in_(codes),
        )
    ).all()

    by_code: dict[str, dict[str, Any]] = {}
    sector_counts: dict[str, int] = {code: 0 for code in codes}
    fetched_max = None
    for code, name, sector_code, fetched_at in member_rows:
        entry = by_code.setdefault(
            code, {"code": code, "name": name, "sector_codes": set()}
        )
        entry["sector_codes"].add(sector_code)
        sector_counts[sector_code] = sector_counts.get(sector_code, 0) + 1
        if fetched_at is not None and (fetched_max is None or fetched_at > fetched_max):
            fetched_max = fetched_at

    for item in selected:
        item["member_count"] = sector_counts.get(item["code"], 0)
        item["has_data"] = item["member_count"] > 0

    missing = [item["code"] for item in selected if not item["has_data"]]
    warnings = list(base["warnings"])
    reasons: list[str] = ["NO_MEMBERS_COMPLETION_LOG", "OBSERVED_ONLY"]
    if missing:
        warnings.append("SELECTED_SECTOR_NOT_COVERED")
        reasons.append("SELECTED_SECTOR_NOT_COVERED")
    if len(taxonomies) > 1:
        warnings.append("TAXONOMY_MIXED")
        reasons.append("TAXONOMY_MIXED")

    matched_codes: list[str]
    if mode == "AND" and missing:
        warnings.append("AND_UNVERIFIABLE")
        reasons.append("AND_UNVERIFIABLE")
        matched_codes = []
    else:
        if mode == "AND":
            matched_codes = [
                code
                for code, entry in by_code.items()
                if all(sector in entry["sector_codes"] for sector in codes)
            ]
        else:
            matched_codes = list(by_code)
    if market != "all":
        matched_codes = [code for code in matched_codes if _market_of(code) == market]
        warnings.append("MARKET_FILTER_APPLIED")

    matched_codes.sort()
    total = len(matched_codes)
    page_codes = matched_codes[offset : offset + limit]

    # ---------------- D 日池（可信度先判，再分桶）
    up_trusted, up_reason = _pool_trust(
        session, resolved, "pool_up", "up", allow_empty=False
    )
    broken_trusted, broken_reason = _pool_trust(
        session, resolved, "pool_broken", "broken", allow_empty=True
    )
    if not up_trusted:
        warnings.append("POOL_UP_UNVERIFIED")
        reasons.append("POOL_UP_UNVERIFIED")
    if not broken_trusted:
        warnings.append("POOL_BROKEN_UNVERIFIED")
        reasons.append("POOL_BROKEN_UNVERIFIED")

    up_rows: dict[str, LimitPool] = {}
    if page_codes:
        for row in session.scalars(
            select(LimitPool).where(
                LimitPool.trade_date == resolved, LimitPool.code.in_(page_codes)
            )
        ):
            if row.pool_type == "up":
                up_rows[row.code] = row

    # 桶计数按**全部匹配股**算（不是只算当前页），否则分页会改变统计
    pool_membership: dict[str, set[str]] = {}
    height_by_code: dict[str, int | None] = {}
    if matched_codes:
        for row in session.scalars(
            select(LimitPool).where(
                LimitPool.trade_date == resolved, LimitPool.code.in_(matched_codes)
            )
        ):
            pool_membership.setdefault(row.code, set()).add(row.pool_type)
            if row.pool_type == "up":
                height_by_code[row.code] = row.consecutive

    def state_of(code: str) -> str:
        pools = pool_membership.get(code, set())
        # R2-P2-01：FIRST/SECOND/THIRD_PLUS/BROKEN/CONFLICT 都是**互斥分类**，
        # 必须由 up 与 broken 两个来源同时可信支撑；任一来源失败/缺日志时，
        # 不能把「另一池未知」当成「另一池确定不存在」。
        if not (up_trusted and broken_trusted):
            return STATE_UNKNOWN
        if "up" in pools and "broken" in pools:
            # 双源可信下的冲突：单独列出，不按 if/elif 优先级择一
            return STATE_CONFLICT
        if "broken" in pools:
            return STATE_BROKEN
        if "up" in pools:
            height = height_by_code.get(code)
            if height == 1:
                return STATE_FIRST
            if height == 2:
                return STATE_SECOND
            if height is not None and height >= 3:
                return STATE_THIRD_PLUS
            return STATE_UNKNOWN
        # 不在任何池里：双源都可信时才是「确定没涨停/没炸板」
        return "none"

    state_counts = {key: 0 for key in STATE_LABELS}
    state_by_code: dict[str, str] = {}
    for code in matched_codes:
        state = state_of(code)
        state_by_code[code] = state
        if state in state_counts:
            state_counts[state] += 1
    if state_counts[STATE_CONFLICT]:
        warnings.append("POOL_CONFLICT")
        reasons.append("POOL_CONFLICT")

    # ---------------- D+1 描述统计
    quotes = _quotes(session, following, matched_codes) if following else {}
    # 有效样本掩码：行情行存在 ≠ 涨跌幅可用；列表、汇总、分桶共用这一份
    valid_quote_codes = {
        code
        for code in matched_codes
        if snd._finite((quotes.get(code) or {}).get("pct_chg")) is not None
    }
    data_as_of = session.scalar(select(func.max(StockDaily.trade_date)))
    next_up_codes: set[str] | None = None
    if following is None:
        outcome_status = STATUS_PENDING
        outcome_reasons = ["NO_NEXT_TRADE_DAY"]
    else:
        next_up_trusted, next_up_reason = _pool_trust(
            session, following, "pool_up", "up", allow_empty=False
        )
        if not next_up_trusted:
            outcome_reasons = ["OUTCOME_LIMIT_POOL_UNVERIFIED"]
        else:
            outcome_reasons = []
            next_up_codes = set(
                session.scalars(
                    select(LimitPool.code).where(
                        LimitPool.trade_date == following,
                        LimitPool.pool_type == "up",
                        LimitPool.code.in_(matched_codes),
                    )
                )
            )
        if data_as_of is None or data_as_of < following:
            outcome_status = STATUS_PENDING
            outcome_reasons = [*outcome_reasons, "OUTCOME_PENDING"]
        elif not quotes:
            outcome_status = STATUS_UNVERIFIABLE
            outcome_reasons = [*outcome_reasons, "OUTCOME_PARTIAL"]
        elif len(valid_quote_codes) < total:
            outcome_status = STATUS_PARTIAL
            outcome_reasons = [*outcome_reasons, "OUTCOME_PARTIAL"]
        else:
            outcome_status = STATUS_VERIFIED if next_up_trusted else STATUS_PARTIAL

    outcome = (
        _stats_block(matched_codes, quotes, limit_up_codes=next_up_codes)
        if outcome_status not in (STATUS_PENDING,)
        else _empty_stats()
    )
    if outcome_status == STATUS_PENDING:
        outcome["sample_count"] = total
        outcome["missing_count"] = total
        outcome["unverifiable_count"] = 0

    by_state: dict[str, Any] = {}
    for state, label in STATE_LABELS.items():
        group_codes = [code for code in matched_codes if state_by_code.get(code) == state]
        block = _stats_block(group_codes, quotes, limit_up_codes=next_up_codes)
        block["label"] = label
        by_state[state] = block

    # ---------------- 逐券行（当前页）
    rows: list[dict[str, Any]] = []
    for code in page_codes:
        entry = by_code.get(code, {"name": None, "sector_codes": set()})
        quote = quotes.get(code) if following else None
        state = state_by_code.get(code, STATE_UNKNOWN)
        # 与统计分母同一掩码：涨跌幅缺失时不输出「是否再涨停」布尔值
        again = (
            (code in next_up_codes)
            if next_up_codes is not None and code in valid_quote_codes
            else None
        )
        rows.append(
            {
                "code": code,
                "name": entry.get("name"),
                "sector_codes": sorted(entry["sector_codes"]),
                "sector_count": len(entry["sector_codes"]),
                "state": state,
                "state_label": STATE_LABELS.get(state, "未涨停/未炸板"),
                "consecutive": height_by_code.get(code),
                "first_seal_time": (
                    up_rows[code].first_seal_time if code in up_rows else None
                ),
                "pct_chg": (up_rows[code].pct_chg if code in up_rows else None),
                "outcome_pct_chg": (quote or {}).get("pct_chg"),
                "outcome_close": (quote or {}).get("close"),
                "limit_up_again": again,
            }
        )

    # ---------------- 市场情绪
    sentiment = session.get(MarketSentiment, resolved)
    market_sentiment = (
        {
            "limit_up_count": sentiment.limit_up_count,
            "broken_rate": sentiment.broken_rate,
            "max_consecutive": sentiment.max_consecutive,
        }
        if sentiment is not None
        else None
    )

    known_by = _snapshot_known_by(fetched_max, resolved)
    pit = {
        "historical_signal_eligible": False,
        "member_snapshot_known_by": known_by,
        "known_by_observation_time": (
            True if known_by == "PRIOR_DAY" else False if known_by == "LATER" else None
        ),
        "later_snapshot_for_review": known_by == "LATER",
    }

    observed_limit_up = state_counts[STATE_FIRST] + state_counts[STATE_SECOND] + state_counts[STATE_THIRD_PLUS]
    observed_ratio = (
        round(observed_limit_up / total * 100, 2) if total and up_trusted else None
    )
    quality_status = STATUS_PARTIAL
    if mode == "AND" and missing:
        quality_status = STATUS_UNVERIFIABLE
    if not up_trusted:
        quality_status = STATUS_UNVERIFIABLE

    base.update(
        {
            "coverage": {
                "member_observed_rows": len(member_rows),
                "member_unique_count": len(by_code),
                "member_completion_proof": False,
                "observed_sectors": sum(1 for item in selected if item["has_data"]),
                "expected_sectors": len(codes),
                "observed_limit_up_count": observed_limit_up if up_trusted else None,
                "observed_limit_up_ratio": observed_ratio,
                "ratio_scope": "OBSERVED_SAMPLE_ONLY",
                "note": REASON_TEXT["NO_MEMBERS_COMPLETION_LOG"],
            },
            "pit": pit,
            "market_sentiment": market_sentiment,
            "state_counts": state_counts,
            "outcome": outcome,
            "outcome_status": outcome_status,
            "outcome_reason_codes": outcome_reasons,
            "by_state": by_state,
            "total": total,
            "shown": len(page_codes),
            "has_more": offset + len(page_codes) < total,
            "quality": {
                "status": quality_status,
                "reasons": list(dict.fromkeys(reasons)),
            },
            "warnings": list(dict.fromkeys(warnings)),
            "rows": rows,
        }
    )
    return base
