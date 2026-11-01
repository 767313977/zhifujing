"""做 T 换算的边界（2026-11-01 审计 P2-1）。

复现场景：买入日在 `stock_daily` 里没有行 → `load_enriched` 把 `adjusted` 置空；
此时「盘中当场记的 T」日期晚于窗口最后一天，旧实现会走「按最后一天的复权比例估算」
分支并取 `adjusted[-1]` → IndexError → 交易日志接口整体 500（新增完 T 之后连列表都打不开）。

口径：**换算不了就返回 None**（与「该日早于窗口但缺日线」一致），不猜。
"""

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import (
    AppUser,
    Base,
    StockDaily,
    TradeCalendar,
    TradeJournal,
    TradeT,
)
from app.services.trade_journal import load_enriched

ENTRY = date(2026, 10, 30)
BAR_DAY = date(2026, 11, 2)
T_DAY = date(2026, 11, 3)


def _engine_with_trade() -> tuple:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(AppUser(id=1, username="u", password_hash="x"))
    session.add_all(
        [TradeCalendar(trade_date=day) for day in (ENTRY, BAR_DAY, T_DAY)]
    )
    # 只有 11-02 一根日线，买入日（10-30）缺 → adjusted 会被置空
    session.add(
        StockDaily(
            trade_date=BAR_DAY,
            code="600519",
            name="贵州茅台",
            open=10.0,
            high=10.0,
            low=10.0,
            close=10.0,
            pct_chg=0.0,
            volume=1e6,
            amount=1e7,
        )
    )
    trade = TradeJournal(
        user_id=1,
        code="600519",
        entry_date=ENTRY,
        entry_price=100.0,
        position_pct=10.0,
        reason_type="other",
    )
    session.add(trade)
    session.flush()
    session.add(
        TradeT(
            user_id=1,
            trade_id=trade.id,
            trade_date=T_DAY,
            direction="long",
            buy_price=101.0,
            sell_price=102.0,
            ratio_pct=50.0,
        )
    )
    session.commit()
    return engine, session


def test_t_leg_after_window_without_entry_bar_does_not_crash():
    """买入日缺日线 + T 记在窗口之后：不能抛 IndexError，换算不了就置 None。"""
    _, session = _engine_with_trade()
    try:
        rows = load_enriched(session, 1)
        assert len(rows) == 1
        t_ops = rows[0]["t_ops"]
        assert len(t_ops) == 1
        assert t_ops[0]["spread_pct"] is None
        assert t_ops[0]["contrib_pct"] is None
    finally:
        session.close()


def test_t_leg_within_window_with_empty_adjusted_returns_none():
    """买入日缺日线但 T 在窗口内：同样只是算不出来，不应抛。"""
    engine, session = _engine_with_trade()
    try:
        session.query(TradeT).update({TradeT.trade_date: BAR_DAY})
        session.commit()
        rows = load_enriched(session, 1)
        assert rows[0]["t_ops"][0]["spread_pct"] is None
    finally:
        session.close()
        engine.dispose()
