"""历史相似日接口（2026-10-18 清单 #4）。全部本地计算，零配额。"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser
from app.schemas import SimilarDaysOut
from app.services import similar_days as similar_days_service
from app.services.auth import current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/similar-days", tags=["历史相似日"])


@router.get("", response_model=SimilarDaysOut)
def similar_days(
    target: date | None = Query(None, alias="date", description="以哪一天为「今天」，缺省最新"),
    top: int = Query(10, ge=5, le=20),
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> SimilarDaysOut:
    """用情绪向量找历史上最像的几天，并列出它们之后 1/3/5 日的指数与情绪。

    向量：涨停数 / 炸板率 / 最高板 / 站上 MA20 / 恐贪，各维先转成历史分位再加权。
    相似度**只作参照**，不是预测。`date` 传非交易日时退到之前最近一个有情绪数据的日子。
    """
    return SimilarDaysOut.model_validate(
        similar_days_service.similar(session, target, top)
    )
