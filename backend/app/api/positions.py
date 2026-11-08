"""持仓接口（FEAT-POSITIONS，2026-10-31）。

持仓 = `trade_journal` 里 `exit_date` 为空的记录，**不新建表**。
这里只做增删改查与「从计划转入」；平仓仍走交易日志页。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser, TradeJournal, TradePlan
from app.schemas import PositionIn, PositionOut, PositionRow
from app.services import positions, trade_journal
from app.services.auth import current_user

router = APIRouter(prefix="/api/positions", tags=["持仓"])


def _get_open(session: Session, user: AppUser, position_id: int) -> TradeJournal:
    journal = session.get(TradeJournal, position_id)
    if journal is None or journal.user_id != user.id or journal.exit_date is not None:
        raise HTTPException(status_code=404, detail="这条持仓不存在（或已平仓）")
    return journal


def _row(session: Session, user: AppUser, journal: TradeJournal) -> PositionRow:
    rows = {row["id"]: row for row in positions.load(session, user.id)}
    data = rows.get(journal.id)
    if data is None:
        raise HTTPException(status_code=404, detail="这条持仓不存在")
    return PositionRow.model_validate(data)


@router.get("", response_model=PositionOut)
def list_positions(
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> PositionOut:
    """我的持仓：未平仓记录 + 本地最新收盘与浮盈。纯读库，不联网。"""
    return PositionOut(
        rows=[PositionRow.model_validate(row) for row in positions.load(session, user.id)]
    )


@router.post("", response_model=PositionRow)
def create_position(
    payload: PositionIn,
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> PositionRow:
    """手工建一笔持仓。"""
    try:
        journal = positions.create(
            session,
            user.id,
            code=payload.code,
            entry_date=payload.entry_date,
            entry_price=payload.entry_price,
            name=payload.name,
            shares=payload.shares,
            position_pct=payload.position_pct,
            note=payload.note,
            plan_id=payload.plan_id,
        )
    except positions.PlanJournalClosed as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _row(session, user, journal)


@router.post("/from-plan/{plan_id}", response_model=PositionRow)
def create_from_plan(
    plan_id: int,
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> PositionRow:
    """把一条计划转成持仓：成本取回填的实际价，没回填就用计划买点。

    **幂等**（2026-11-08 审计 P1-01）：同一条计划再点一次，返回已有的那笔持仓，
    不会插第二笔（服务层 + 唯一索引双保险）。
    **一个事务**（P2-06）：持仓与计划状态一起提交 —— 以前分成两次 commit，
    第二步失败就留下「有持仓、计划还没标执行」的半成品。
    """
    plan = session.get(TradePlan, plan_id)
    if plan is None or plan.user_id != user.id:
        raise HTTPException(status_code=404, detail="这条计划不存在")
    try:
        journal = positions.create(
            session,
            user.id,
            code=plan.code,
            entry_date=plan.plan_date,
            entry_price=plan.actual_price or plan.buy_price,
            name=plan.name,
            position_pct=None,
            note=(plan.position_note or plan.note),
            plan_id=plan.id,
            commit=False,
        )
    except positions.PlanJournalClosed as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    plan.status = "executed"
    plan.status_source = "manual"
    if plan.actual_price is None:
        plan.actual_price = plan.buy_price
        plan.followed_plan = True
    session.commit()
    return _row(session, user, journal)


@router.put("/{position_id}", response_model=PositionRow)
def update_position(
    position_id: int,
    payload: PositionIn,
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> PositionRow:
    journal = _get_open(session, user, position_id)
    try:
        code = positions.validate_code(session, payload.code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 与新建 / 交易日志 / 计划执行同一份校验（交易日、不晚于今天、股数、仓位）
    try:
        trade_journal.validate_entry(
            session,
            entry_date=payload.entry_date,
            entry_price=float(payload.entry_price),
            shares=payload.shares,
            position_pct=payload.position_pct,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    journal.code = code
    journal.name = (payload.name or "").strip() or journal.name
    journal.entry_date = payload.entry_date
    journal.entry_price = float(payload.entry_price)
    journal.shares = payload.shares
    journal.position_pct = payload.position_pct
    journal.note = (payload.note or "").strip() or None
    session.commit()
    return _row(session, user, journal)


@router.delete("/{position_id}")
def delete_position(
    position_id: int,
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> dict:
    journal = _get_open(session, user, position_id)
    session.delete(journal)
    session.commit()
    return {"ok": True}
