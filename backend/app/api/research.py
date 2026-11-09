"""P0.2 研究接口：趋势 / 低位启动 / 板块状态。

只读、受现有鉴权保护；不触发外部采集；不改变任何正式 track 路由。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser
from app.services import data_revision, research_screen
from app.services.auth import current_user

router = APIRouter(prefix="/api/research", tags=["研究"])


@router.get("/meta")
def meta(user: AppUser = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """研究策略版本与验证状态，前端用来显示“研究指标 ≠ 已验证策略”。

    `cache` 只返回给管理员：普通用户不需要、也不应该看到库身份/修订号等内部诊断。
    无论是否管理员，缓存不可用都会写服务端日志（60 秒节流）。
    """
    state = data_revision.status(db)
    if not state.ready:
        data_revision.log_unavailable(state)
    payload: dict = {
        "trend": research_screen.TREND_STRATEGY,
        "low_start": research_screen.LOW_START_STRATEGY,
        "sector_state": research_screen.SECTOR_STATE_STRATEGY,
    }
    if user.is_admin:
        payload["cache"] = {
            "state": state.state,
            "reason": state.reason,
            "identity": state.identity[2] if state.identity else None,
            "revisions": dict(state.version[1]) if state.version else None,
            # 进程内计数：重启归零；用于确认「这次请求是否命中缓存」
            "stats": research_screen.cache_stats(),
        }
    return payload


@router.get("/trend")
def trend(
    trade_date: date | None = Query(None, alias="date"),
    min_rps60: float = Query(60, ge=0, le=100),
    min_trend_days: int = Query(5, ge=0, le=250),
    limit: int = Query(100, ge=1, le=200),
    exclude_st: bool = Query(True),
    exclude_bse: bool = Query(True),
    db: Session = Depends(get_db),
) -> dict:
    try:
        return research_screen.trend_rows(
            db,
            trade_date=trade_date,
            min_rps60=min_rps60,
            min_trend_days=min_trend_days,
            limit=limit,
            exclude_st=exclude_st,
            exclude_bse=exclude_bse,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/low-start")
def low_start(
    trade_date: date | None = Query(None, alias="date"),
    limit: int = Query(100, ge=1, le=200),
    max_position: float = Query(40, ge=0, le=100),
    min_platform_days: int = Query(15, ge=5, le=120),
    max_platform_height: float = Query(18, ge=1, le=100),
    max_vol_contraction: float = Query(0.9, ge=0.1, le=3),
    exclude_st: bool = Query(True),
    exclude_bse: bool = Query(True),
    db: Session = Depends(get_db),
) -> dict:
    try:
        return research_screen.low_start_rows(
            db,
            trade_date=trade_date,
            limit=limit,
            max_position=max_position,
            min_platform_days=min_platform_days,
            max_platform_height=max_platform_height,
            max_vol_contraction=max_vol_contraction,
            exclude_st=exclude_st,
            exclude_bse=exclude_bse,
        )
    except research_screen.ResearchBusy as exc:
        # 全市场扫描并发已满且等待超时：明确告诉客户端稍后重试，而不是无限排队
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/sector-state")
def sector_state(
    trade_date: date | None = Query(None, alias="date"),
    taxonomy: str = Query("kph_selected", pattern="^(kph_selected|kph_industry)$"),
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    try:
        return research_screen.sector_states(
            db, trade_date=trade_date, taxonomy=taxonomy, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
