"""板块强度：RET20/60、RPS20/60、排名变化、广度、集中度、持续性。

口径与 `sector_rps.board` **完全一致**（F4/F5 修完后）：

- 收益：板块 `pct_chg` 复利，窗口按**板块交易日**截取，覆盖率 < 90% 记 None；
- 分位：`rps.mid_rank`（并列同分），横截面是「当天所有有值的板块」；
- 广度：只在 `sector_member` 的 manifest 完整且与来源 Count 一致时可信，
  否则 `up_ratio` / `strong_members` 一律 None（不拿残缺成分冒充广度）。

⚠️ 本模块只读库，不写库。板块**不做硬过滤**：模型默认给 0 分，可在 0~5 分配置。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import RpsDaily, SectorDaily, SectorMember, TradeCalendar
from app.services import research_screen
from app.sources.base import chunked
from app.services.trend_engine import config as cfg_module

logger = logging.getLogger(__name__)

#: 板块强度的计算版本。改窗口/覆盖率/分位口径必须升。
CALC_VERSION = "sector-strength-1.0.0"


def _sector_days(session: Session, day: date, taxonomy: str, count: int) -> list[date]:
    rows = list(
        session.scalars(
            select(SectorDaily.trade_date)
            .where(SectorDaily.taxonomy == taxonomy, SectorDaily.trade_date <= day)
            .group_by(SectorDaily.trade_date)
            .order_by(SectorDaily.trade_date.desc())
            .limit(count)
        )
    )
    return sorted(rows)


def _series(
    session: Session, days: list[date], taxonomy: str
) -> dict[str, dict[date, tuple[float | None, float | None]]]:
    if not days:
        return {}
    rows = session.execute(
        select(
            SectorDaily.sector_code,
            SectorDaily.trade_date,
            SectorDaily.pct_chg,
            SectorDaily.amount,
        ).where(
            SectorDaily.taxonomy == taxonomy,
            SectorDaily.trade_date.in_(days),
        )
    ).all()
    out: dict[str, dict[date, tuple[float | None, float | None]]] = {}
    for code, when, pct, amount in rows:
        out.setdefault(code, {})[when] = (pct, amount)
    return out


def _rps_map_at(
    aligned: dict[str, list[float | None]],
    end_index: int,
    window: int,
) -> dict[str, float]:
    values: dict[str, float] = {}
    for code, series in aligned.items():
        value = research_screen._compound_return(series[: end_index + 1], window)
        if value is not None:
            values[code] = value
    if len(values) < 5:
        return {}
    return research_screen._mid_rank_map(values)


def _ranked(rps_map: dict[str, float]) -> dict[str, int]:
    ordered = sorted(rps_map.items(), key=lambda item: (-item[1], item[0]))
    return {code: index + 1 for index, (code, _value) in enumerate(ordered)}


@dataclass(frozen=True, slots=True)
class SectorStrengthBoard:
    trade_date: str
    requested_date: str
    actual_data_date: str
    taxonomy: str
    rows: list[dict]
    coverage: dict
    calc_version: str = CALC_VERSION


def evaluate(
    session: Session,
    *,
    trade_date: date | None = None,
    taxonomy: str = "kph_selected",
    limit: int = 100,
    config: cfg_module.SectorConfig | None = None,
) -> SectorStrengthBoard:
    config = config or cfg_module.SectorConfig()
    requested = trade_date or session.scalar(
        select(func.max(SectorDaily.trade_date)).where(SectorDaily.taxonomy == taxonomy)
    )
    if requested is None:
        return SectorStrengthBoard("", "", "", taxonomy, [], {"status": "no_data"})

    actual = session.scalar(
        select(func.max(SectorDaily.trade_date)).where(
            SectorDaily.taxonomy == taxonomy, SectorDaily.trade_date <= requested
        )
    )
    if actual is None:
        return SectorStrengthBoard(
            str(requested), str(requested), "", taxonomy, [], {"status": "no_data"}
        )

    history = max(config.windows) + config.rank_change_days + config.persistence_days + 2
    days = _sector_days(session, actual, taxonomy, history)
    series = _series(session, days, taxonomy)
    if not series:
        return SectorStrengthBoard(
            str(actual), str(requested), str(actual), taxonomy, [], {"status": "no_data"}
        )

    aligned = {
        code: [values.get(day, (None, None))[0] for day in days]
        for code, values in series.items()
    }
    index = len(days) - 1
    ret20_map = {
        code: value
        for code, value in (
            (code, research_screen._compound_return(values, 20))
            for code, values in aligned.items()
        )
        if value is not None
    }
    ret60_map = {
        code: value
        for code, value in (
            (code, research_screen._compound_return(values, 60))
            for code, values in aligned.items()
        )
        if value is not None
    }
    rps20_map = _rps_map_at(aligned, index, 20)
    rps60_map = _rps_map_at(aligned, index, 60)
    rank_now = _ranked(rps20_map)
    prev_index = max(index - config.rank_change_days, 0)
    rank_prev = _ranked(_rps_map_at(aligned, prev_index, 20)) if prev_index < index else {}

    # ---- 广度：只有 manifest 完整才可信 ----
    member_counts = dict(
        session.execute(
            select(SectorMember.sector_code, func.count())
            .where(SectorMember.trade_date == actual)
            .group_by(SectorMember.sector_code)
        ).all()
    )
    manifest = research_screen._member_manifest(session, actual)
    members = session.execute(
        select(SectorMember.code, SectorMember.sector_code, SectorMember.pct_chg).where(
            SectorMember.trade_date == actual
        )
    ).all()
    member_pcts: dict[str, list[float]] = {}
    member_codes: dict[str, list[str]] = {}
    for code, board, pct in members:
        member_codes.setdefault(board, []).append(code)
        if pct is not None:
            member_pcts.setdefault(board, []).append(float(pct))

    all_member_codes = [code for codes in member_codes.values() for code in codes]
    member_rps60: dict[str, float] = {}
    # 一个板块可能 1000+ 成分股，全市场 6 万参数会撞 SQLite 变量上限 → 必须分批
    for batch in chunked(sorted(set(all_member_codes)), 900):
        for code, value in session.execute(
            select(RpsDaily.code, RpsDaily.rps60).where(
                RpsDaily.trade_date == actual, RpsDaily.code.in_(batch)
            )
        ).all():
            if value is not None:
                member_rps60[code] = float(value)

    # ---- 持续性：RPS20 ≥ 阈值连续多少天（含当日）----
    rps_cache: dict[int, dict[str, float]] = {index: rps20_map}

    def rps_at(position: int) -> dict[str, float]:
        if position not in rps_cache:
            rps_cache[position] = _rps_map_at(aligned, position, 20)
        return rps_cache[position]

    names = {
        code: name
        for code, name in session.execute(
            select(SectorDaily.sector_code, SectorDaily.name).where(
                SectorDaily.trade_date == actual, SectorDaily.taxonomy == taxonomy
            )
        ).all()
    }

    rows: list[dict] = []
    trusted_boards = 0
    for code, value in rps20_map.items():
        actual_rows = int(member_counts.get(code, 0))
        status, reason = research_screen._breadth_status(
            manifest is not None, (manifest or {}).get(code), actual_rows
        )
        trusted = status == research_screen.MEMBER_COMPLETE
        if trusted:
            trusted_boards += 1
        pcts = member_pcts.get(code, [])
        up_ratio = (
            round(sum(1 for item in pcts if item > 0) / len(pcts) * 100, 1)
            if trusted and pcts
            else None
        )
        strong_members = None
        if trusted:
            strong_members = sum(
                1
                for member in member_codes.get(code, [])
                if member_rps60.get(member, -1) >= config.member_strong_rps
            )
        positive = [item for item in pcts if item > 0]
        concentration = None
        if trusted and positive:
            top5 = sum(sorted(positive, reverse=True)[:5])
            concentration = round(top5 / sum(positive), 3)

        persistence = 0
        for offset in range(config.persistence_days):
            position = index - offset
            if position < 0:
                break
            if rps_at(position).get(code, -1) >= config.strong_rps:
                persistence += 1
            else:
                break

        prev_rank = rank_prev.get(code)
        rows.append(
            {
                "sector_code": code,
                "name": names.get(code),
                "trade_date": str(actual),
                "ret20": ret20_map.get(code),
                "ret60": ret60_map.get(code),
                "rps20": round(value, 1),
                "rps60": round(rps60_map[code], 1) if code in rps60_map else None,
                "rank": rank_now.get(code),
                "prev_rank": prev_rank,
                "rank_change": (prev_rank - rank_now[code]) if prev_rank and code in rank_now else None,
                "up_ratio": up_ratio,
                "strong_members": strong_members,
                "member_count": actual_rows or None,
                "persistence_days": persistence,
                "concentration_top5": concentration,
                "breadth_status": status,
                "missing": [] if trusted else [f"广度不可信：{reason}"],
            }
        )

    rows.sort(key=lambda item: (-(item["rps20"] or -1), item["sector_code"]))
    coverage = {
        "status": "ok" if actual == requested else "fallback",
        "days": len(days),
        "boards": len(series),
        "boards_with_rps20": len(rps20_map),
        "boards_with_rps60": len(rps60_map),
        "breadth_trusted": trusted_boards,
        "manifest_installed": manifest is not None,
        "note": "板块广度只在 manifest 完整时可信；RPS/收益不受成分股影响。",
    }
    return SectorStrengthBoard(
        trade_date=str(actual),
        requested_date=str(requested),
        actual_data_date=str(actual),
        taxonomy=taxonomy,
        rows=rows[:limit],
        coverage=coverage,
    )


def sector_score(rows: list[dict], code: str) -> float | None:
    """把板块行的 RPS20 转成 0~100 的板块分（模型用）。

    只取 RPS20；没有该板块 → None（不是 0，避免「未知 = 弱」）。
    """
    for row in rows:
        if row.get("sector_code") == code:
            value = row.get("rps20")
            return None if value is None else float(value)
    return None


def resolve_sector_code(
    session: Session, day: date, code: str, taxonomy: str = "kph_selected"
) -> str | None:
    """个股当日所属板块（取成交额最大的那个）。同一股票可能属于多个概念，只能选一个主板块。"""
    rows = session.execute(
        select(SectorMember.sector_code)
        .join(
            SectorDaily,
            (SectorDaily.sector_code == SectorMember.sector_code)
            & (SectorDaily.trade_date == SectorMember.trade_date),
        )
        .where(
            SectorMember.trade_date == day,
            SectorMember.code == code,
            SectorDaily.taxonomy == taxonomy,
        )
        .order_by(SectorDaily.amount.desc())
        .limit(1)
    ).first()
    return rows[0] if rows else None


def trading_day_count(session: Session, start: date, end: date) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(TradeCalendar)
            .where(TradeCalendar.trade_date >= start, TradeCalendar.trade_date <= end)
        )
        or 0
    )
