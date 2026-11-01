"""持仓（FEAT-POSITIONS，2026-10-31）。

**复用 `trade_journal` 的未平仓记录**：一行持仓 = 一笔 `exit_date` 为空的交易日志，
不另建 position 表（用户已确认「持仓复用日志」，避免两套账）。

- 现价取本地最新 `stock_daily` 收盘（与自选、盘前同一口径），**不联网**；
- 浮盈 = (现价 − 成本) × 数量；没有数量时只给比例；
- 平仓仍然在交易日志页做（这里只增删改未平仓记录，不动已平仓的）。
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import StockBasic, TradeJournal, TradePlan
from app.services.watch_scope import latest_quotes


def _row(journal: TradeJournal, quote) -> dict:
    close = quote.close if quote is not None else None
    pnl = None
    pnl_pct = None
    value = None
    if close is not None:
        pnl_pct = round((close / journal.entry_price - 1) * 100, 2)
        if journal.shares:
            pnl = round((close - journal.entry_price) * journal.shares, 2)
            value = round(close * journal.shares, 2)
    return {
        "id": journal.id,
        "code": journal.code,
        "name": journal.name,
        "entry_date": journal.entry_date,
        "entry_price": journal.entry_price,
        "shares": journal.shares,
        "position_pct": journal.position_pct,
        "note": journal.note,
        "plan_id": journal.plan_id,
        "latest_date": quote.trade_date if quote is not None else None,
        "latest_close": close,
        "market_value": value,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
    }


def load(session: Session, user_id: int) -> list[dict]:
    """当前用户的未平仓记录（= 持仓），按建仓日倒序。"""
    journals = list(
        session.scalars(
            select(TradeJournal)
            .where(TradeJournal.user_id == user_id, TradeJournal.exit_date.is_(None))
            .order_by(TradeJournal.entry_date.desc(), TradeJournal.id.desc())
        )
    )
    if not journals:
        return []
    quotes = latest_quotes(session, [journal.code for journal in journals])
    return [_row(journal, quotes.get(journal.code)) for journal in journals]


def validate_code(session: Session, code: str) -> str:
    digits = "".join(char for char in str(code or "").strip() if char.isdigit())[:6]
    if len(digits) != 6:
        raise ValueError("股票代码需要 6 位数字")
    if session.get(StockBasic, digits) is None:
        raise ValueError(f"查不到这个代码「{digits}」")
    return digits


def create(
    session: Session,
    user_id: int,
    *,
    code: str,
    entry_date: date,
    entry_price: float,
    name: str | None = None,
    shares: int | None = None,
    position_pct: float | None = None,
    note: str | None = None,
    plan_id: int | None = None,
) -> TradeJournal:
    digits = validate_code(session, code)
    if entry_price <= 0:
        raise ValueError("成本价必须大于 0")
    if plan_id is not None:
        # 归属校验：手工建仓也能带 plan_id，不校验就能把持仓挂到别人的计划 id 上，
        # 计划页的「已执行 / 跟随情况」统计会被污染（无数据泄露，但口径全错）。
        plan = session.get(TradePlan, plan_id)
        if plan is None or plan.user_id != user_id:
            raise ValueError("关联的计划不存在")
    basic = session.get(StockBasic, digits)
    journal = TradeJournal(
        user_id=user_id,
        code=digits,
        name=(name or "").strip() or (basic.name if basic else None),
        entry_date=entry_date,
        entry_price=float(entry_price),
        shares=shares,
        position_pct=position_pct,
        plan_id=plan_id,
        reason_type="other",
        note=(note or "").strip() or None,
    )
    session.add(journal)
    session.commit()
    session.refresh(journal)
    return journal


def update(journal: TradeJournal, **fields) -> TradeJournal:
    for key, value in fields.items():
        if value is not None:
            setattr(journal, key, value)
    return journal
