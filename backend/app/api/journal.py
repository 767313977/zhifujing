"""交易日志接口：增删改查 + 按理由 / 情绪 / 板块的成绩统计。

收益口径全部在 `services/trade_journal.py`（用 `stock_daily` 现算），这里只做
入参校验与持久化。**不占 iFinD 配额**，也不发外部请求。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser, StockBasic, TradeCalendar, TradeJournal
from app.schemas import TradeJournalIn, TradeJournalOut, TradeJournalRow, TradeStatsOut
from app.services import trade_journal
from app.services.auth import current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/journal", tags=["交易日志"])


def _normalize_code(code: str) -> str:
    text = str(code or "").strip()
    # 允许用户粘贴「600519.SH」这类写法：只取前 6 位数字
    digits = "".join(char for char in text if char.isdigit())[:6]
    if len(digits) != 6:
        raise HTTPException(status_code=400, detail="股票代码需要 6 位数字")
    return digits


def _require_trade_day(session: Session, day, label: str) -> None:
    exists = session.scalar(
        select(TradeCalendar.trade_date).where(TradeCalendar.trade_date == day)
    )
    if exists is None:
        raise HTTPException(status_code=400, detail=f"{label} {day} 不是交易日")


def _apply(session: Session, trade: TradeJournal, payload: TradeJournalIn) -> None:
    """校验并入参 → 写回模型。新增与修改共用。"""
    code = _normalize_code(payload.code)
    if payload.entry_price <= 0:
        raise HTTPException(status_code=400, detail="买入价必须大于 0")
    if payload.exit_price is not None and payload.exit_price <= 0:
        raise HTTPException(status_code=400, detail="卖出价必须大于 0")
    if payload.plan_stop is not None and payload.plan_stop <= 0:
        raise HTTPException(status_code=400, detail="止损位必须大于 0")
    if payload.position_pct is not None and not (0 <= payload.position_pct <= 100):
        raise HTTPException(status_code=400, detail="仓位需要在 0-100 之间")
    if (payload.exit_date is None) != (payload.exit_price is None):
        raise HTTPException(status_code=400, detail="卖出日期与卖出价要同时填或同时留空")
    if payload.exit_date is not None and payload.exit_date < payload.entry_date:
        raise HTTPException(status_code=400, detail="卖出日期不能早于买入日期")
    if payload.reason_type not in trade_journal.REASON_TYPES:
        raise HTTPException(status_code=400, detail="理由类型不合法")
    if payload.reason_type in ("pattern", "wudao") and not payload.reason_key:
        raise HTTPException(status_code=400, detail="请选择具体的形态 / 悟道池")

    _require_trade_day(session, payload.entry_date, "买入日")
    if payload.exit_date is not None:
        _require_trade_day(session, payload.exit_date, "卖出日")

    name = (payload.name or "").strip() or None
    if name is None:
        basic = session.get(StockBasic, code)
        name = basic.name if basic is not None else None

    trade.code = code
    trade.name = name
    trade.entry_date = payload.entry_date
    trade.entry_price = float(payload.entry_price)
    trade.exit_date = payload.exit_date
    trade.exit_price = float(payload.exit_price) if payload.exit_price else None
    trade.position_pct = payload.position_pct
    trade.reason_type = payload.reason_type
    trade.reason_key = (payload.reason_key or "").strip() or None
    trade.reason_text = (payload.reason_text or "").strip() or None
    trade.plan_stop = float(payload.plan_stop) if payload.plan_stop else None
    trade.note = (payload.note or "").strip() or None


def _row(session: Session, user: AppUser, trade_id: int) -> TradeJournalRow:
    rows = trade_journal.load_enriched(session, user.id)
    for row in rows:
        if row["id"] == trade_id:
            return TradeJournalRow.model_validate(row)
    raise HTTPException(status_code=404, detail="这笔交易不存在")


@router.get("", response_model=TradeJournalOut)
def list_trades(
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> TradeJournalOut:
    """该用户的全部交易（含现算收益 / 回撤 / 持有天数 / 止损执行），按买入日倒序。"""
    rows = trade_journal.load_enriched(session, user.id)
    return TradeJournalOut(rows=[TradeJournalRow.model_validate(row) for row in rows])


@router.post("", response_model=TradeJournalRow)
def create_trade(
    payload: TradeJournalIn,
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> TradeJournalRow:
    trade = TradeJournal(user_id=user.id)
    _apply(session, trade, payload)
    session.add(trade)
    session.commit()
    session.refresh(trade)
    return _row(session, user, trade.id)


@router.put("/{trade_id}", response_model=TradeJournalRow)
def update_trade(
    trade_id: int,
    payload: TradeJournalIn,
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> TradeJournalRow:
    trade = session.get(TradeJournal, trade_id)
    if trade is None or trade.user_id != user.id:
        raise HTTPException(status_code=404, detail="这笔交易不存在")
    _apply(session, trade, payload)
    session.commit()
    return _row(session, user, trade.id)


@router.delete("/{trade_id}")
def delete_trade(
    trade_id: int,
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> dict:
    trade = session.get(TradeJournal, trade_id)
    if trade is None or trade.user_id != user.id:
        raise HTTPException(status_code=404, detail="这笔交易不存在")
    session.delete(trade)
    session.commit()
    return {"ok": True}


@router.get("/stats", response_model=TradeStatsOut)
def trade_stats(
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> TradeStatsOut:
    """按理由 / 情绪档位 / 板块分组看自己的胜率与平均收益（只统计已平仓）。"""
    return TradeStatsOut.model_validate(trade_journal.stats(session, user.id))
