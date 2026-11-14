"""盘后数据可信度控制台（S4-B）—— 只读诊断接口。

- 只 `SELECT` 本地表；不发外部请求、不补采、不建表、不修库；
- 强制登录（`current_user`），与全站鉴权同一套；
- `days` 有上限（250），超限 400，避免一次扫描整库历史。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.external_guard import stats as egress_stats
from app.models import AppUser
from app.schemas import QualityOverviewOut, QualityTimelineOut
from app.services import data_quality
from app.services.auth import current_user

router = APIRouter(prefix="/api/quality", tags=["数据质量"])


@router.get("/overview", response_model=QualityOverviewOut)
def overview(
    date: date | None = Query(None, description="观察日；不传用今天"),
    days: int = Query(30, ge=data_quality.MIN_DAYS, le=data_quality.MAX_DAYS),
    session: Session = Depends(get_db),
    _user: AppUser = Depends(current_user),
) -> QualityOverviewOut:
    """某个交易日、六个本地来源的覆盖 / 可信度 / 缺口原因。"""
    try:
        data = data_quality.overview(session, target_date=date, days=days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return QualityOverviewOut(**data)


@router.get("/egress")
def egress(_user: AppUser = Depends(current_user)) -> dict:
    """离线闸门计数（隔离验收用）：尝试 / 被阻断 / 真正发网 / 被网络层拦截。

    `network_attempts` 是**真正发出去**的请求数（开关打开时才可能非零）；
    开关关闭时它必须恒为 0。
    """
    return egress_stats()


@router.get("/timeline", response_model=QualityTimelineOut)
def timeline(
    source: str = Query(..., description="来源名，见 /overview 的 sources"),
    days: int = Query(120, ge=data_quality.MIN_DAYS, le=data_quality.MAX_DAYS),
    session: Session = Depends(get_db),
    _user: AppUser = Depends(current_user),
) -> QualityTimelineOut:
    """单个来源的逐日状态（只读），用于判断缺口是偶发还是持续。"""
    try:
        data = data_quality.timeline(session, source=source, days=days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return QualityTimelineOut(**data)
