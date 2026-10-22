"""次日计划接口：增删改查 + 自选/形态候选（2026-10-18 清单 #2）。

验证结果（是否触发 / 触发后走势）在 `services/trade_plan.py`，这里只管入参与持久化。
同一用户、同一只票、同一个计划日只留一条：重复保存按更新处理（唯一索引兜底）。
"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser, StockBasic, TradeCalendar, TradePlan
from app.schemas import (
    TradePlanIn,
    TradePlanOut,
    TradePlanRow,
    TradePlanSuggestionsOut,
)
from app.services import trade_plan
from app.services.auth import current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/plans", tags=["次日计划"])


def _normalize_code(code: str) -> str:
    digits = "".join(char for char in str(code or "").strip() if char.isdigit())[:6]
    if len(digits) != 6:
        raise HTTPException(status_code=400, detail="股票代码需要 6 位数字")
    return digits


def _validate(session: Session, payload: TradePlanIn) -> tuple[str, str | None]:
    if payload.buy_price <= 0 or payload.stop_price <= 0:
        raise HTTPException(status_code=400, detail="买点与放弃位都必须大于 0")
    if payload.stop_price >= payload.buy_price:
        raise HTTPException(status_code=400, detail="放弃位要低于买点（过 X 买、破 Y 放弃）")
    exists = session.scalar(
        select(TradeCalendar.trade_date).where(
            TradeCalendar.trade_date == payload.plan_date
        )
    )
    if exists is None:
        raise HTTPException(status_code=400, detail=f"{payload.plan_date} 不是交易日")
    code = _normalize_code(payload.code)
    name = (payload.name or "").strip() or None
    if name is None:
        basic = session.get(StockBasic, code)
        name = basic.name if basic is not None else None
    return code, name


@router.get("", response_model=TradePlanOut)
def list_plans(
    plan_date: date | None = Query(None, alias="date"),
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> TradePlanOut:
    """该用户的计划（不传日期就是全部），含是否触发 / 触发后走势。"""
    rows = trade_plan.load(session, user.id, plan_date)
    return TradePlanOut(rows=[TradePlanRow.model_validate(row) for row in rows])


@router.get("/suggestions", response_model=TradePlanSuggestionsOut)
def suggestions(
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> TradePlanSuggestionsOut:
    """次日计划的候选：自选股 + 当日评分最高的形态命中（带出 key_levels）。"""
    return TradePlanSuggestionsOut.model_validate(
        {"rows": trade_plan.suggestions(session, user.id)}
    )


def _fill(plan: TradePlan, name: str | None, payload: TradePlanIn) -> None:
    """把入参写回计划行：新增、并发冲突后改走「更新」、修改三处共用这一份。"""
    plan.name = name
    plan.buy_price = float(payload.buy_price)
    plan.stop_price = float(payload.stop_price)
    plan.note = (payload.note or "").strip() or None


@router.post("", response_model=TradePlanRow)
def create_plan(
    payload: TradePlanIn,
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> TradePlanRow:
    code, name = _validate(session, payload)
    plan = session.scalar(
        select(TradePlan).where(
            TradePlan.user_id == user.id,
            TradePlan.code == code,
            TradePlan.plan_date == payload.plan_date,
        )
    )
    if plan is None:
        plan = TradePlan(user_id=user.id, code=code, plan_date=payload.plan_date)
        session.add(plan)
    _fill(plan, name, payload)
    try:
        session.commit()
    except IntegrityError:
        # 并发连点 / 两个标签页同时保存：两个请求都在上面「查不到」，后者 INSERT 撞
        # `ux_trade_plan_user_code_date`。这不是错误 —— 结果与串行路径完全一样（那一行
        # 已经在库里），按「更新」重写一次再返回，不要把 500 丢给用户（2026-10-22 修，
        # 与 `api/watchlist.add_watchlist` 的 IntegrityError 处理同一套）。
        session.rollback()
        plan = session.scalar(
            select(TradePlan).where(
                TradePlan.user_id == user.id,
                TradePlan.code == code,
                TradePlan.plan_date == payload.plan_date,
            )
        )
        if plan is None:
            # 理论上不会走到：唯一约束只可能被同一把键挡下
            raise
        _fill(plan, name, payload)
        session.commit()
    session.refresh(plan)
    return _row(session, user, plan.id)


@router.put("/{plan_id}", response_model=TradePlanRow)
def update_plan(
    plan_id: int,
    payload: TradePlanIn,
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> TradePlanRow:
    code, name = _validate(session, payload)
    plan = session.get(TradePlan, plan_id)
    if plan is None or plan.user_id != user.id:
        raise HTTPException(status_code=404, detail="这条计划不存在")
    # 改成「另一条已有计划」的 code + plan_date 会撞唯一索引
    # `ux_trade_plan_user_code_date`，不拦的话 commit 直接 IntegrityError → 500
    conflict = session.scalar(
        select(TradePlan.id).where(
            TradePlan.user_id == user.id,
            TradePlan.code == code,
            TradePlan.plan_date == payload.plan_date,
            TradePlan.id != plan_id,
        )
    )
    if conflict is not None:
        raise HTTPException(
            status_code=400, detail="该票当天已有一条计划，请直接编辑那一条"
        )
    plan.code = code
    plan.plan_date = payload.plan_date
    _fill(plan, name, payload)
    try:
        session.commit()
    except IntegrityError:
        # 上面的预检挡不住「预检之后、commit 之前别人刚插了同键行」这一瞬：改走
        # 「另一条已有计划」的 code+plan_date 仍会撞唯一索引。这里把它转成与预检
        # 同一句 400，不冒 500（2026-10-22 加）。
        session.rollback()
        raise HTTPException(
            status_code=400, detail="该票当天已有一条计划，请直接编辑那一条"
        ) from None
    return _row(session, user, plan.id)


@router.delete("/{plan_id}")
def delete_plan(
    plan_id: int,
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> dict:
    plan = session.get(TradePlan, plan_id)
    if plan is None or plan.user_id != user.id:
        raise HTTPException(status_code=404, detail="这条计划不存在")
    session.delete(plan)
    session.commit()
    return {"ok": True}


def _row(session: Session, user: AppUser, plan_id: int) -> TradePlanRow:
    for row in trade_plan.load(session, user.id):
        if row["id"] == plan_id:
            return TradePlanRow.model_validate(row)
    raise HTTPException(status_code=404, detail="这条计划不存在")
