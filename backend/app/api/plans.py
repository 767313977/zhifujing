"""次日计划接口：增删改查 + 自选/形态候选（2026-10-18 清单 #2）。

验证结果（是否触发 / 触发后走势）在 `services/trade_plan.py`，这里只管入参与持久化。
同一用户、同一只票、同一个计划日只留一条：重复保存按更新处理（唯一索引兜底）。
"""

import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser, StockBasic, TradeJournal, TradePlan
from app.schemas import (
    PlanExecuteIn,
    PlanExecuteOut,
    PlanFromSignalIn,
    PlanFromSignalOut,
    PlanStatsOut,
    PlanStatusIn,
    TradePlanIn,
    TradePlanOut,
    TradePlanRow,
    TradePlanSuggestionsOut,
)
from app.services import trade_plan
from app.services.auth import current_user
from app.services.trading_days import is_trade_day
from app.timeutil import today_cn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/plans", tags=["次日计划"])


#: 单个用户最多保留多少条计划 —— 与自选股 `MAX_WATCHLIST` 同一套做法
#: （2026-11-01 审计 P2-2）。按「代码 × 日期」一天一条，200 条足够覆盖大半年。
MAX_PLANS = 200


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
    if payload.buy_high is not None and payload.buy_high < payload.buy_price:
        raise HTTPException(status_code=400, detail="买入价区上限不能低于触发价")
    code = _normalize_code(payload.code)
    # 代码必须真在 `stock_basic` 里（2026-10-23 体检修，文案与
    # `api/watchlist.add_watchlist` 对齐）：`_normalize_code` 只校验"6 位数字"，
    # 手误的 999999 也能落一条永远「无日线」的计划行，用户还以为存上了。
    basic = session.get(StockBasic, code)
    if basic is None:
        raise HTTPException(
            status_code=400, detail=f"查不到这个代码「{code}」，请确认后重试"
        )
    if not is_trade_day(payload.plan_date, session):
        raise HTTPException(status_code=400, detail=f"{payload.plan_date} 不是交易日")
    name = (payload.name or "").strip() or None
    if name is None:
        name = basic.name
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


@router.post("/from-signal", response_model=PlanFromSignalOut)
def create_from_signal(
    payload: PlanFromSignalIn,
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> PlanFromSignalOut:
    """从形态 / 悟道名单一键生成计划（FEAT-PLAN-LOOP A）。

    默认不覆盖同一 (用户, 代码, 执行日) 的既有计划：用户可能已经改过价位，
    重复点击只把原计划原样返回（`created=false`）。
    """
    try:
        row, created = trade_plan.create_from_signal(
            session,
            user.id,
            payload.code,
            payload.pattern,
            signal_date=payload.signal_date,
            plan_date=payload.plan_date,
            overwrite=payload.overwrite,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return PlanFromSignalOut(
        created=created, row=TradePlanRow.model_validate(row)
    )


@router.get("/stats", response_model=PlanStatsOut)
def plan_stats(
    date_from: date | None = Query(None, alias="from", description="起始计划日（含）"),
    date_to: date | None = Query(None, alias="to", description="结束计划日（含）"),
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> PlanStatsOut:
    """区间执行质量：计划数 / 执行率 / 按计划执行率 / 关联日志盈亏（有则算）。"""
    end = date_to or today_cn()
    start = date_from or (end - timedelta(days=30))
    if start > end:
        raise HTTPException(status_code=400, detail="起始日期不能晚于结束日期")
    return PlanStatsOut(**trade_plan.stats(session, user.id, start, end))


def _fill(plan: TradePlan, name: str | None, payload: TradePlanIn) -> None:
    """把入参写回计划行：新增、并发冲突后改走「更新」、修改三处共用这一份。

    来源类字段（source / signal_date / ref_price）**空值不覆盖**：手工编辑时表单
    不带这些字段，直接写 None 会把「从哪份名单来的」抹掉。
    """
    plan.name = name
    plan.buy_price = float(payload.buy_price)
    plan.stop_price = float(payload.stop_price)
    plan.note = (payload.note or "").strip() or None
    plan.source = payload.source or plan.source
    plan.source_name = payload.source_name or plan.source_name
    plan.signal_date = payload.signal_date or plan.signal_date
    if payload.ref_price is not None:
        plan.ref_price = float(payload.ref_price)
    plan.buy_high = float(payload.buy_high) if payload.buy_high is not None else None
    plan.target_price = (
        float(payload.target_price) if payload.target_price is not None else None
    )
    plan.position_note = (payload.position_note or "").strip() or None
    plan.observe_only = bool(payload.observe_only)
    if payload.actual_price is not None:
        plan.actual_price = float(payload.actual_price)
    if payload.followed_plan is not None:
        plan.followed_plan = bool(payload.followed_plan)
    plan.deviation = (payload.deviation or "").strip() or None


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
        # 行数上限（2026-11-01 审计 P2-2）：只有**新增**才数 —— 同一天同一只票是
        # upsert（已有行就改写），不该被上限挡掉。
        count = (
            session.scalar(
                select(func.count())
                .select_from(TradePlan)
                .where(TradePlan.user_id == user.id)
            )
            or 0
        )
        if count >= MAX_PLANS:
            raise HTTPException(
                status_code=400,
                detail=f"交易计划最多 {MAX_PLANS} 条，你现在有 {count} 条，先删掉一些再加",
            )
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
    return _row(session, user, plan.id, plan.plan_date)


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
    return _row(session, user, plan.id, plan.plan_date)


@router.post("/{plan_id}/execute", response_model=PlanExecuteOut)
def execute_plan(
    plan_id: int,
    payload: PlanExecuteIn,
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> PlanExecuteOut:
    """回填实际买入价 + 是否按计划，并写一笔关联交易日志（FEAT-PLAN-LOOP C）。

    同一条计划重复提交时**更新原日志**，不会生成多笔 —— 否则周度执行率会把
    同一次交易算成两次。
    """
    plan = session.get(TradePlan, plan_id)
    if plan is None or plan.user_id != user.id:
        raise HTTPException(status_code=404, detail="这条计划不存在")
    if payload.actual_price <= 0:
        raise HTTPException(status_code=400, detail="实际买入价必须大于 0")
    entry_date = payload.entry_date or plan.plan_date
    if entry_date > today_cn():
        raise HTTPException(status_code=400, detail="成交日不能晚于今天")

    plan.actual_price = float(payload.actual_price)
    plan.followed_plan = bool(payload.followed_plan)
    plan.deviation = (payload.deviation or "").strip() or None
    # 手动回填 = 已执行，且锁定状态：自动核对不再把它改回「过期」
    plan.status = "executed"
    plan.status_source = "manual"

    reason_type = "other"
    if plan.source:
        reason_type = "wudao" if plan.source.startswith("wudao") else "pattern"
    journal = session.scalar(
        select(TradeJournal)
        .where(TradeJournal.user_id == user.id, TradeJournal.plan_id == plan.id)
        .order_by(TradeJournal.id.desc())
        .limit(1)
    )
    if journal is None:
        journal = TradeJournal(
            user_id=user.id,
            code=plan.code,
            name=plan.name,
            entry_date=entry_date,
            entry_price=float(payload.actual_price),
            shares=payload.shares,
            position_pct=payload.position_pct,
            reason_type=reason_type,
            reason_key=plan.source,
            reason_text=plan.deviation,
            plan_stop=plan.stop_price,
            plan_id=plan.id,
            note=(payload.note or "").strip() or None,
        )
        session.add(journal)
    else:
        journal.entry_date = entry_date
        journal.entry_price = float(payload.actual_price)
        journal.plan_stop = plan.stop_price
        if plan.deviation:
            journal.reason_text = plan.deviation
        if payload.shares is not None:
            journal.shares = payload.shares
        if payload.position_pct is not None:
            journal.position_pct = payload.position_pct
        if payload.note:
            journal.note = payload.note.strip() or journal.note
    session.commit()
    session.refresh(journal)
    return PlanExecuteOut(
        row=_row(session, user, plan.id, plan.plan_date), journal_id=journal.id
    )


@router.put("/{plan_id}/status", response_model=TradePlanRow)
def set_plan_status(
    plan_id: int,
    payload: PlanStatusIn,
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> TradePlanRow:
    """手动改计划状态；传 `status=null` 交回自动同步（FEAT-PLAN-LOOP B）。"""
    status = payload.status
    if status is not None and status not in trade_plan.PLAN_STATUSES:
        raise HTTPException(status_code=400, detail=f"未知状态：{status}")
    plan = session.get(TradePlan, plan_id)
    if plan is None or plan.user_id != user.id:
        raise HTTPException(status_code=404, detail="这条计划不存在")
    plan.status = status
    plan.status_source = "manual" if status is not None else None
    session.commit()
    return _row(session, user, plan.id, plan.plan_date)


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


def _row(
    session: Session, user: AppUser, plan_id: int, plan_date: date
) -> TradePlanRow:
    """保存后回一行。

    ⚠️ 只按**这条计划的计划日**取（2026-10-28 体检）：原来调 `trade_plan.load(session, user.id)`
    把该用户**全部**计划连同验证（每条都要读日线、算 1/3/5 日收益）重算一遍，只为了挑
    出刚保存的那一条 —— 计划攒到几十条时，每次点保存都要多等几百毫秒。计划日相同的
    计划通常只有几条，口径与列表页完全一致（同一个 `load`）。
    """
    rows = trade_plan.load(session, user.id, plan_date)
    for row in rows:
        if row["id"] == plan_id:
            return TradePlanRow.model_validate(row)
    raise HTTPException(status_code=404, detail="这条计划不存在")
