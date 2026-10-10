"""复盘笔记接口。

每天一条：市场观点 + 次日计划。数据落在 `review_note` 表，
**按用户隔离**（2026-09-28 起，见设计文档 §8.69）：主键是 `(user_id, trade_date)`，
所以「取某天的笔记」必须同时给用户 id。
"""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser, ReviewNote
from app.schemas import NoteIn, NoteOut
from app.services.auth import current_user

router = APIRouter(prefix="/api/note", tags=["复盘笔记"])

# 笔记日期的合理区间。下限 2020-01-01（本站在此之前没有数据可复盘）；
# 上限是「今天 + 7 天」——留出一周是因为前端允许提前写次日 / 下周计划。
# ⚠️ 上限跟着「今天」走，**不能写成固定常量，也就不能用 `Path(le=...)`** ——
# 那样要么写死一个未来日期、要么直接禁掉明天，所以用手写校验（见 _check_date）。
NOTE_MIN_DATE = date(2020, 1, 1)
NOTE_FUTURE_DAYS = 7


def _check_date(trade_date: date) -> None:
    """把笔记日期限在合理区间，越界抛 400（两个端点都要过）。

    不卡范围的话，一个登录用户能把 `0001-01-01` 到 `9999-12-31` 每天都写一条、
    每条最多 4 万字 ×2 段 —— 光靠一个人就能把库刷满（路径参数 `date` 本身不设界）。
    """
    latest = date.today() + timedelta(days=NOTE_FUTURE_DAYS)
    if trade_date < NOTE_MIN_DATE or trade_date > latest:
        raise HTTPException(
            status_code=400,
            detail=(
                f"日期只能填 {NOTE_MIN_DATE} 到 {latest}（今天 + {NOTE_FUTURE_DAYS} 天）之间，"
                f"收到「{trade_date}」"
            ),
        )


@router.get("/{trade_date}", response_model=NoteOut)
def get_note(
    trade_date: date,
    user: AppUser = Depends(current_user),
    session: Session = Depends(get_db),
) -> NoteOut:
    """取某天的笔记。没写过就返回空模板，前端不必区分「没写过」与「写空了」。"""
    _check_date(trade_date)
    row = session.get(ReviewNote, (user.id, trade_date))
    if row is None:
        return NoteOut(
            trade_date=trade_date, market_view=None, next_plan=None, updated_at=None
        )
    return NoteOut.model_validate(row)


@router.put("/{trade_date}", response_model=NoteOut)
def save_note(
    trade_date: date,
    payload: NoteIn,
    user: AppUser = Depends(current_user),
    session: Session = Depends(get_db),
) -> NoteOut:
    _check_date(trade_date)
    row = session.get(ReviewNote, (user.id, trade_date))
    if row is None:
        row = ReviewNote(user_id=user.id, trade_date=trade_date)
        session.add(row)
    row.market_view = payload.market_view
    row.next_plan = payload.next_plan
    session.commit()
    session.refresh(row)
    return NoteOut.model_validate(row)
