"""复盘笔记接口。

每天一条：市场观点 + 次日计划。数据落在 `review_note` 表，
**按用户隔离**（2026-09-28 起，见设计文档 §8.69）：主键是 `(user_id, trade_date)`，
所以「取某天的笔记」必须同时给用户 id。
"""

from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser, ReviewNote
from app.schemas import NoteIn, NoteOut
from app.services.auth import current_user

router = APIRouter(prefix="/api/note", tags=["复盘笔记"])


@router.get("/{trade_date}", response_model=NoteOut)
def get_note(
    trade_date: date,
    user: AppUser = Depends(current_user),
    session: Session = Depends(get_db),
) -> NoteOut:
    """取某天的笔记。没写过就返回空模板，前端不必区分「没写过」与「写空了」。"""
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
    row = session.get(ReviewNote, (user.id, trade_date))
    if row is None:
        row = ReviewNote(user_id=user.id, trade_date=trade_date)
        session.add(row)
    row.market_view = payload.market_view
    row.next_plan = payload.next_plan
    session.commit()
    session.refresh(row)
    return NoteOut.model_validate(row)
