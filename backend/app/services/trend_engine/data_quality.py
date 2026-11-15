"""数据质量闸门（F2）：把「RPS 混版本 / 孤儿日 / 覆盖率不足」变成可读状态。

背景（M0 审计）：

- `rps_daily` 历史上由不同版本的 `scan_rps` 写入，2026-10-21 那天的 `ret20/ret60`
  全为空、`ret120/ret250` 有值；同一天**晚于**个股日线末日 → 孤儿 RPS；
- 任何跨日研究如果直接读 RPS，会把「旧版本只写了一部分窗口」当成有效分位。

本模块只做**只读检查**：给定交易日，返回该日 RPS 是否可用于研究，以及原因。
调用方（回测 / API / 页面）在 `ok=False` 时应拒绝把该日结果当成有效，而不是填 0 或猜。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import RpsDaily, SectorDaily, StockDaily

#: 与 `services.rps.MIN_COVERAGE` 同口径
MIN_COVERAGE = 0.9


@dataclass(frozen=True, slots=True)
class RpsDayStatus:
    trade_date: date
    daily_rows: int
    rps_rows: int
    rps20_rows: int
    rps60_rows: int
    coverage: float | None
    ok: bool
    status: str
    notes: tuple[str, ...] = field(default_factory=tuple)


def rps_day_status(session: Session, day: date) -> RpsDayStatus:
    daily_rows = int(
        session.scalar(
            select(func.count()).select_from(StockDaily).where(StockDaily.trade_date == day)
        )
        or 0
    )
    rps_rows = int(
        session.scalar(
            select(func.count()).select_from(RpsDaily).where(RpsDaily.trade_date == day)
        )
        or 0
    )
    rps20_rows = int(
        session.scalar(
            select(func.count())
            .select_from(RpsDaily)
            .where(RpsDaily.trade_date == day, RpsDaily.rps20.is_not(None))
        )
        or 0
    )
    rps60_rows = int(
        session.scalar(
            select(func.count())
            .select_from(RpsDaily)
            .where(RpsDaily.trade_date == day, RpsDaily.rps60.is_not(None))
        )
        or 0
    )
    coverage = (rps60_rows / daily_rows) if daily_rows else None
    notes: list[str] = []

    if daily_rows == 0 and rps_rows > 0:
        return RpsDayStatus(
            day, daily_rows, rps_rows, rps20_rows, rps60_rows, None, False,
            "orphan_rps", ("该日有 RPS 但没有任何个股日线（孤儿 RPS，可能是旧版本写入）",)
        )
    if daily_rows == 0:
        return RpsDayStatus(day, 0, rps_rows, rps20_rows, rps60_rows, None, False,
                            "no_bars", ("该日没有个股日线",))
    if rps_rows == 0:
        return RpsDayStatus(day, daily_rows, 0, 0, 0, None, False,
                            "no_rps", ("该日有日线但没有 RPS",))
    if rps60_rows == 0:
        notes.append("rps60 全为空：旧版本只写了 120/250 档")
        return RpsDayStatus(day, daily_rows, rps_rows, rps20_rows, 0, None, False,
                            "no_rps60", tuple(notes))
    if rps20_rows == 0:
        notes.append("rps20 全为空：旧版本数据")
    if coverage is not None and coverage < MIN_COVERAGE:
        notes.append(f"rps60 覆盖率 {coverage:.1%} < {MIN_COVERAGE:.0%}，横截面分位不可比")
        return RpsDayStatus(day, daily_rows, rps_rows, rps20_rows, rps60_rows,
                            round(coverage, 4), False, "partial_rps", tuple(notes))
    return RpsDayStatus(day, daily_rows, rps_rows, rps20_rows, rps60_rows,
                        round(coverage, 4) if coverage is not None else None, True, "ok", tuple(notes))


def coverage_flags(session: Session, day: date, taxonomy: str = "kph_selected") -> dict:
    """一个更容易被 API 直接返回的汇总：RPS + 板块 + 指数日期是否对齐。"""
    rps = rps_day_status(session, day)
    sector_day = session.scalar(
        select(func.max(SectorDaily.trade_date)).where(
            SectorDaily.taxonomy == taxonomy, SectorDaily.trade_date <= day
        )
    )
    return {
        "trade_date": str(day),
        "rps": {
            "status": rps.status,
            "ok": rps.ok,
            "coverage": rps.coverage,
            "notes": list(rps.notes),
        },
        "sector_actual_date": str(sector_day) if sector_day else None,
        "sector_stale": bool(sector_day is None or sector_day != day),
    }
