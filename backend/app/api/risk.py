"""风控提醒接口（FEAT-RISK-GUARD，2026-10-31）。

`/pref` 读写用户偏好；`/status` 现算「是否过热 / 集中度是否超限」。
**只提示，不阻止操作**；全部读本地库，零外部请求。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser
from app.schemas import RiskPrefIn, RiskStatusOut
from app.services import risk
from app.services.auth import current_user

router = APIRouter(prefix="/api/risk", tags=["风控"])


@router.get("/pref", response_model=RiskPrefIn)
def get_pref(
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> RiskPrefIn:
    return RiskPrefIn(**risk.get_pref(session, user.id))


@router.put("/pref", response_model=RiskPrefIn)
def save_pref(
    payload: RiskPrefIn,
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> RiskPrefIn:
    return RiskPrefIn(**risk.save_pref(session, user.id, payload.model_dump()))


@router.get("/status", response_model=RiskStatusOut)
def get_status(
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> RiskStatusOut:
    """当前是否过热 / 是否触发集中度提醒。没有触发时 `message` 也是可读的。"""
    return RiskStatusOut(**risk.status(session, user.id))
