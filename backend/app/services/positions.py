"""持仓（FEAT-POSITIONS，2026-10-31）。

**复用 `trade_journal` 的未平仓记录**：一行持仓 = 一笔 `exit_date` 为空的交易日志，
不另建 position 表（用户已确认「持仓复用日志」，避免两套账）。

- 现价取本地最新 `stock_daily` 收盘（与自选、盘前同一口径），**不联网**；
- 浮盈按**买入日口径**（`trade_journal.open_basis`，按 `pct_chg` 复利折算）计算，
  与交易日志页同一个数；直接用不复权收盘价遇到分红送转会显示假亏损
  （2026-11-01 审计 P1）；
- 市值与浮盈**同一口径**：都按「买入日口径价 × 股数」算 —— `shares` 的含义是
  **买入时的股数**。这样「市值 = 成本 + 浮盈」在送转前后都成立。代价是除权后它不再
  等于交易所里的真实市值（那需要按除权后的股数换算），这是刻意的取舍（2026-11-01 复查 P3）；
- 平仓仍然在交易日志页做（这里只增删改未平仓记录，不动已平仓的）。
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import StockBasic, TradeJournal, TradePlan
from app.services import trade_journal
from app.services.watch_scope import latest_quotes


def _row(journal: TradeJournal, quote, basis: float | None) -> dict:
    close = quote.close if quote is not None else None
    # 行情日期早于买入日 = 本地日线还没覆盖买入日（当天刚建仓、停牌、补采缺口）。
    # 拿更早的收盘价当现价会算出「昨天的浮盈」，口径跨了一天 —— 宁可不给数
    # （2026-11-08 审计 P2-02）。最新日期照常带出，页面上能看出行情停在哪天。
    stale = quote is not None and quote.trade_date < journal.entry_date
    if stale:
        close = None
    # 买入日口径的等价价；买入日还没日线时退回不复权收盘（那时也没有除权信息可用）
    price = basis if basis is not None else close
    pnl = None
    pnl_pct = None
    value = None
    if price is not None:
        pnl_pct = round((price / journal.entry_price - 1) * 100, 2)
        if journal.shares:
            pnl = round((price - journal.entry_price) * journal.shares, 2)
    # 市值与浮盈必须同一个价：用不复权收盘的话，送转后会出现
    # 「市值 ≠ 成本 + 浮盈」（2026-11-01 复查 P3）
    if price is not None and journal.shares:
        value = round(price * journal.shares, 2)
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
    bases = trade_journal.open_basis(session, journals)
    return [
        _row(journal, quotes.get(journal.code), bases.get(journal.id))
        for journal in journals
    ]


def validate_code(session: Session, code: str) -> str:
    digits = "".join(char for char in str(code or "").strip() if char.isdigit())[:6]
    if len(digits) != 6:
        raise ValueError("股票代码需要 6 位数字")
    if session.get(StockBasic, digits) is None:
        raise ValueError(f"查不到这个代码「{digits}」")
    return digits


class PlanJournalClosed(Exception):
    """这条计划已经有一笔**已平仓**的日志，不能再转成持仓。

    2026-11-08 第二轮复审 P2-NEW-03：以前直接返回那笔已平仓记录，随后计划被标成
    「已执行」，API 再从「未平仓列表」里取它 → 404；更糟的是重复转持仓看起来
    「成功了」但持仓页永远看不到。现在明确 409，且计划 / 交易都不动。
    """


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
    commit: bool = True,
) -> TradeJournal:
    """建一笔持仓（= 一条未平仓的交易日志）。

    **一条计划最多一笔**（2026-11-08 审计 P1-01）：带 `plan_id` 时先查是否已有
    日志，有就直接返回那一笔（幂等）；并发仍可能同时看不到，靠
    `ux_trade_journal_user_plan` 这个唯一索引兜底 —— 撞上了回滚再查一次。
    **已平仓**的日志不能再当成「持仓」返回，抛 `PlanJournalClosed`（API 层 409）。

    `commit=False` 给「要和别的写同一事务」的调用方用（如 `from-plan` 要同时改
    计划状态，见 P2-06）。插入本身已在该事务里执行，由调用方统一提交。
    """
    digits = validate_code(session, code)
    # 与交易日志 / 计划执行共用同一份校验：交易日、不晚于今天、股数 > 0、仓位 0-100
    trade_journal.validate_entry(
        session,
        entry_date=entry_date,
        entry_price=float(entry_price),
        shares=shares,
        position_pct=position_pct,
    )
    if plan_id is not None:
        # 归属校验：手工建仓也能带 plan_id，不校验就能把持仓挂到别人的计划 id 上，
        # 计划页的「已执行 / 跟随情况」统计会被污染（无数据泄露，但口径全错）。
        plan = session.get(TradePlan, plan_id)
        if plan is None or plan.user_id != user_id:
            raise ValueError("关联的计划不存在")
        existing = _journal_of_plan(session, user_id, plan_id)
        if existing is not None:
            if existing.exit_date is not None:
                raise PlanJournalClosed(
                    "这条计划关联的交易已经平仓，不能再转为持仓；如需重新建仓，请新建一笔交易"
                )
            return existing
    basic = session.get(StockBasic, digits)
    try:
        journal = trade_journal.insert_trade(
            session,
            user_id,
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
        if commit:
            session.commit()
    except trade_journal.JournalLimitExceeded:
        # 499→500 的最后一位被**同一计划**的并发请求先占走：原子插入拿到 0 行，
        # 但目标记录其实已经存在。按业务唯一键重查，返回同一笔（2026-11-08
        # 第三轮复审 P2-C）；plan_id 为空时没有业务键可查，原样抛上限错误。
        if plan_id is None:
            raise
        session.rollback()
        existing = _journal_of_plan(session, user_id, plan_id)
        if existing is None:
            raise
        if existing.exit_date is not None:
            raise PlanJournalClosed(
                "这条计划关联的交易已经平仓，不能再转为持仓；如需重新建仓，请新建一笔交易"
            ) from None
        return existing
    except IntegrityError:
        # 并发下另一个请求已经给这条计划建了日志 —— 回滚后把那一笔查出来返回。
        # 走到这里说明前面的 get-or-create 没看到它（两个请求同时 SELECT 到空）。
        session.rollback()
        existing = (
            _journal_of_plan(session, user_id, plan_id) if plan_id is not None else None
        )
        if existing is None:
            raise
        if existing.exit_date is not None:
            raise PlanJournalClosed(
                "这条计划关联的交易已经平仓，不能再转为持仓；如需重新建仓，请新建一笔交易"
            ) from None
        return existing
    return journal


def _journal_of_plan(session: Session, user_id: int, plan_id: int) -> TradeJournal | None:
    """这条计划已经写过的日志（最多一笔，唯一索引保证）。"""
    return session.scalar(
        select(TradeJournal)
        .where(TradeJournal.user_id == user_id, TradeJournal.plan_id == plan_id)
        .order_by(TradeJournal.id)
        .limit(1)
    )
