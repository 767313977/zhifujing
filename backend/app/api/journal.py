"""交易日志接口：增删改查 + 按理由 / 情绪 / 板块的成绩统计。

收益口径全部在 `services/trade_journal.py`（用 `stock_daily` 现算），这里只做
入参校验与持久化。**不占 iFinD 配额**，也不发外部请求。

**按用户隔离**（2026-10-21 用户要求）：所有读写一律用 `current_user.id` 过滤，
并且逐条校验 `user_id` 归属；管理员**没有**跨用户查看 / 修改的入口 ——
`api/admin.py` 只管理账号（改名、重置密码、停用），不碰 `trade_journal`。
以后加接口时别为了「方便排查」开一个带 `user_id` 参数的旁路。
"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    AppUser,
    PatternHit,
    StockBasic,
    StockConcept,
    StockDaily,
    TradeCalendar,
    TradeJournal,
    TradeT,
)
from app.schemas import (
    JournalReasonHit,
    JournalReasonSuggestion,
    TradeJournalIn,
    TradeJournalOut,
    TradeJournalRow,
    TradeStatsOut,
    TradeTIn,
)
from app.services import trade_journal
from app.services.auth import current_user
from app.services.patterns import PATTERNS
from app.services.stock_phase import load_oversold

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


def _check_t_conflicts(
    session: Session,
    trade_id: int,
    entry_date: date,
    exit_date: date | None,
) -> None:
    """改底仓的买入 / 卖出日时，已有的做 T 不能越界（做 T 记录需求 §2）。

    做 T 的前提是「买入日之后才有底仓可 T」（#3）且不能晚于底仓卖出日（#4）；
    一旦底仓日期改得让某条 T 不满足，就报 400 并列出是哪几条 —— 静默放行会让
    那些 T 的指标算在窗口外，看起来只是「数字不对」，很难往回找。
    """
    rows = list(session.scalars(select(TradeT).where(TradeT.trade_id == trade_id)))
    if not rows:
        return
    out_of_range = [
        row
        for row in rows
        if row.trade_date <= entry_date
        or (
            exit_date is not None
            and (
                row.trade_date > exit_date
                or (row.leg2_date is not None and row.leg2_date > exit_date)
            )
        )
    ]
    if out_of_range:
        names = "、".join(
            f"T#{row.id}（{row.trade_date}）" for row in out_of_range
        )
        raise HTTPException(
            status_code=400,
            detail=f"买入日 / 卖出日改动后这些做 T 越界：{names}，请先调整或删除",
        )
    if exit_date is not None:
        unfinished = [
            row
            for row in rows
            if row.buy_price is None or row.sell_price is None
        ]
        if unfinished:
            names = "、".join(
                f"T#{row.id}（{row.trade_date}）" for row in unfinished
            )
            raise HTTPException(
                status_code=400,
                detail=f"底仓已平仓，但这些做 T 还没补第二腿：{names}，请先补上或删除",
            )


def _apply(session: Session, trade: TradeJournal, payload: TradeJournalIn) -> None:
    """校验并入参 → 写回模型。新增与修改共用。"""
    code = _normalize_code(payload.code)
    if payload.entry_price <= 0:
        raise HTTPException(status_code=400, detail="买入价必须大于 0")
    if payload.shares is not None and payload.shares <= 0:
        raise HTTPException(status_code=400, detail="买入股数必须大于 0")
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

    # 改底仓日期前先看已有的做 T 会不会越界（新增时 trade.id 还是 None，跳过）
    if trade.id is not None:
        # 已有做 T 时不允许换股票：T 的价格是按这只票的日线换算的，换票后全算错
        if code != trade.code:
            t_count = len(
                session.scalars(
                    select(TradeT.id).where(TradeT.trade_id == trade.id)
                ).all()
            )
            if t_count:
                raise HTTPException(
                    status_code=400,
                    detail=f"这笔下面有 {t_count} 条做 T，先删掉再改代码",
                )
        _check_t_conflicts(
            session, trade.id, payload.entry_date, payload.exit_date
        )

    name = (payload.name or "").strip() or None
    if name is None:
        basic = session.get(StockBasic, code)
        name = basic.name if basic is not None else None

    trade.code = code
    trade.name = name
    trade.entry_date = payload.entry_date
    trade.entry_price = float(payload.entry_price)
    trade.shares = payload.shares
    trade.exit_date = payload.exit_date
    trade.exit_price = float(payload.exit_price) if payload.exit_price else None
    trade.position_pct = payload.position_pct
    trade.reason_type = payload.reason_type
    trade.reason_key = (payload.reason_key or "").strip() or None
    trade.reason_text = (payload.reason_text or "").strip() or None
    trade.plan_stop = float(payload.plan_stop) if payload.plan_stop else None
    trade.note = (payload.note or "").strip() or None


def _apply_t(
    session: Session,
    trade: TradeJournal,
    row: TradeT,
    payload: TradeTIn,
) -> None:
    """校验并入参 → 写回一条做 T。新增与修改共用（做 T 记录需求 §2）。"""
    if payload.direction not in trade_journal.T_DIRECTIONS:
        raise HTTPException(status_code=400, detail="方向不合法（正T / 倒T）")
    if payload.trigger not in trade_journal.T_TRIGGERS:
        raise HTTPException(status_code=400, detail="触发类型不合法")
    if not (0 < payload.ratio_pct <= 100):
        raise HTTPException(
            status_code=400, detail="做 T 比例需要在 0-100 之间（1/3 仓填 33）"
        )
    for price in (payload.buy_price, payload.sell_price):
        if price is not None and price <= 0:
            raise HTTPException(status_code=400, detail="买卖价必须大于 0")

    # #3 做 T 用的是昨日及以前的底仓：买入当天的底仓 T+1 才能卖
    _require_trade_day(session, payload.trade_date, "做 T 日期")
    if payload.trade_date <= trade.entry_date:
        raise HTTPException(
            status_code=400, detail="做 T 要用昨日底仓：买入当天的底仓 T+1 才能卖"
        )

    # #5 第一腿必填（正 T 先买、倒 T 先卖）
    if payload.direction == "long" and payload.buy_price is None:
        raise HTTPException(status_code=400, detail="正 T 要填买入价")
    if payload.direction == "short" and payload.sell_price is None:
        raise HTTPException(status_code=400, detail="倒 T 要填卖出价")

    # #6 第二腿的价格与日期同进同退
    second_price = (
        payload.sell_price if payload.direction == "long" else payload.buy_price
    )
    if (second_price is None) != (payload.leg2_date is None):
        raise HTTPException(
            status_code=400, detail="第二腿价格与日期要同时填（没做就都留空）"
        )
    if payload.leg2_date is not None:
        _require_trade_day(session, payload.leg2_date, "第二腿日期")
        if payload.leg2_date < payload.trade_date:
            raise HTTPException(
                status_code=400, detail="第二腿日期不能早于第一腿日期"
            )

    # #4 / #8 底仓已平仓：做 T 不能越界，也不允许留一条没做完的
    if trade.exit_date is not None:
        if payload.trade_date > trade.exit_date or (
            payload.leg2_date is not None and payload.leg2_date > trade.exit_date
        ):
            raise HTTPException(
                status_code=400, detail="做 T 日期不能晚于底仓卖出日"
            )
        if second_price is None:
            raise HTTPException(
                status_code=400, detail="底仓已卖出，这次 T 要补上第二腿（或删掉）"
            )

    row.user_id = trade.user_id
    row.trade_id = trade.id
    row.trade_date = payload.trade_date
    row.leg2_date = payload.leg2_date
    row.direction = payload.direction
    row.buy_price = float(payload.buy_price) if payload.buy_price is not None else None
    row.sell_price = (
        float(payload.sell_price) if payload.sell_price is not None else None
    )
    row.ratio_pct = float(payload.ratio_pct)
    row.trigger = payload.trigger
    row.note = (payload.note or "").strip() or None


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


@router.post("/{trade_id}/t", response_model=TradeJournalRow)
def create_t(
    trade_id: int,
    payload: TradeTIn,
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> TradeJournalRow:
    """给一笔底仓记一次做 T，返回更新后的整行（2026-10-21 做 T 记录需求）。"""
    trade = session.get(TradeJournal, trade_id)
    if trade is None or trade.user_id != user.id:
        raise HTTPException(status_code=404, detail="这笔交易不存在")
    row = TradeT()
    _apply_t(session, trade, row, payload)
    session.add(row)
    session.commit()
    return _row(session, user, trade.id)


@router.put("/t/{t_id}", response_model=TradeJournalRow)
def update_t(
    t_id: int,
    payload: TradeTIn,
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> TradeJournalRow:
    """修改一次做 T（补第二腿、改价格），返回更新后的整行。"""
    row = session.get(TradeT, t_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="这次做 T 不存在")
    trade = session.get(TradeJournal, row.trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="这笔交易不存在")
    _apply_t(session, trade, row, payload)
    session.commit()
    return _row(session, user, trade.id)


@router.delete("/t/{t_id}")
def delete_t(
    t_id: int,
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> dict:
    row = session.get(TradeT, t_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="这次做 T 不存在")
    session.delete(row)
    session.commit()
    return {"ok": True}


@router.get("/stats", response_model=TradeStatsOut)
def trade_stats(
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> TradeStatsOut:
    """按理由 / 情绪档位 / 板块分组看自己的胜率与平均收益（只统计已平仓）。"""
    return TradeStatsOut.model_validate(trade_journal.stats(session, user.id))


@router.get("/suggest", response_model=JournalReasonSuggestion)
def suggest_reason(
    code: str = Query(..., min_length=1, max_length=16, description="6 位股票代码"),
    date: date = Query(..., description="买入日"),
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> JournalReasonSuggestion:
    """按「代码 + 买入日」回溯当天命中的形态与所属题材，供交易日志填理由。

    2026-10-21 用户要求：记一笔交易时，理由来源与具体理由能自动回溯查找 ——
    买入那天这只票到底命中了哪些形态、挂在哪个题材，库里本来就有（形态扫描
    `pattern_hit` + 涨停天梯快照 `stock_concept`），不必凭记忆重选。

    **只读库、不发外部请求**；那天没跑扫描或该票当天没命中时返回空列表 ——
    那是「当天没有可比对的记录」，前端如实提示，不替用户编理由。
    """
    target = _normalize_code(code)
    names = {pattern.key: pattern.name for pattern in PATTERNS}
    hits = session.execute(
        select(PatternHit.pattern, PatternHit.score)
        .where(PatternHit.code == target, PatternHit.trade_date == date)
        .order_by(PatternHit.score.desc())
    ).all()
    # 做 T 表单用来决定触发类型是否默认「超跌」；`load_oversold` 只返回达到阈值的，
    # 没超跌时这里就是 None（前端据此保留「其他」）
    oversold_dev = load_oversold(session, [target]).get(target)
    return JournalReasonSuggestion(
        code=target,
        trade_date=date,
        # 注册表里已删掉的旧 key 不返回（历史命中的残留标签，选它也保存不了）
        patterns=[
            JournalReasonHit(key=key, name=names[key], score=round(float(score), 1))
            for key, score in hits
            if key in names
        ],
        themes=sorted(
            session.scalars(
                select(StockConcept.concept).where(
                    StockConcept.code == target, StockConcept.trade_date == date
                )
            )
        ),
        # 做 T 表单用来决定触发类型是否默认「超跌」；`load_oversold` 只返回达到阈值的，
        # 没超跌时这里就是 None（前端据此保留「其他」）
        oversold_dev=oversold_dev,
        # 乖离是按哪天的日线算的：只有 T 日期正好是这一天，前端才自动预选「超跌」；
        # 补记旧 T 时不能拿今天的乖离去套（复查 #3）
        oversold_date=(
            session.scalar(select(func.max(StockDaily.trade_date)))
            if oversold_dev is not None
            else None
        ),
    )
