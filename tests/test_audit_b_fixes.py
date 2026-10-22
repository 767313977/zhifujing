"""审计 B 修复的钉子（并发冲突 / 行业宽度目标日 / 停牌残行 / ETF 基准日）。

每个用例都对着报告里的一条「机制」：
- #1 `create_plan` / `save_note` 并发撞唯一约束（主键）时不再 500，而是按「更新」返回，
  且全表仍只有一行；
- #3 行业宽度不用 `max(trade_date)`，采集中只有部分股票的「半天」不算目标日；
- #4 停牌残行（只有 close、pct_chg 为空）不能当最新一根：自选与个股页取数都要跳过；
- #5 ETF 基准日跳过「碎片快照日」，且非交易日快照不能当基准。
"""

from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api import funds as funds_api
from app.api import market as market_api
from app.api import note as note_api
from app.api import plans as plans_api
from app.api import stock as stock_api
from app.api import watchlist as watchlist_api
from app.models import (
    AppUser,
    Base,
    EtfShare,
    ReviewNote,
    StockBasic,
    StockDaily,
    StockUniverse,
    TradeCalendar,
    TradePlan,
)
from app.schemas import NoteIn, TradePlanIn
from app.services import etf_flow
from app.timeutil import today_cn

DAY = date(2026, 10, 9)


@pytest.fixture()
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'audit-b.db'}", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as setup:
        setup.add(AppUser(username="tester", password_hash="x"))
        setup.add(TradeCalendar(trade_date=DAY))
        setup.add(StockBasic(code="600519", name="贵州茅台"))
        setup.commit()
    return engine


def _racing_session_local(bind, race):
    """第一次 `commit` 之前先让「另一个请求」提交冲突行，模拟并发连点。"""
    state = {"pending": True}

    class RacingSession(Session):
        def commit(self):
            if state["pending"]:
                state["pending"] = False
                race(bind)
            super().commit()

    return sessionmaker(bind=bind, class_=RacingSession, expire_on_commit=False)


# ------------------------------------------------------------------ #1 并发 500


def test_create_plan_race_returns_existing_row(engine):
    def race(bind):
        with Session(bind) as other:
            other.add(
                TradePlan(
                    user_id=1,
                    code="600519",
                    plan_date=DAY,
                    name="旧值",
                    buy_price=90.0,
                    stop_price=80.0,
                )
            )
            other.commit()

    with _racing_session_local(engine, race)() as session:
        user = session.get(AppUser, 1)
        payload = TradePlanIn(
            code="600519",
            name="贵州茅台",
            plan_date=DAY,
            buy_price=100.0,
            stop_price=95.0,
            note="并发保存",
        )
        row = plans_api.create_plan(payload, session=session, user=user)
        assert row.buy_price == 100.0
        assert row.stop_price == 95.0
        stored = session.scalars(select(TradePlan)).all()
        assert len(stored) == 1  # 没有因为并发变成两行
        assert stored[0].id == row.id
        assert stored[0].buy_price == 100.0


def test_save_note_race_returns_updated_row(engine):
    day = today_cn()

    def race(bind):
        with Session(bind) as other:
            other.add(ReviewNote(user_id=1, trade_date=day, market_view="别人先写的"))
            other.commit()

    with _racing_session_local(engine, race)() as session:
        user = session.get(AppUser, 1)
        out = note_api.save_note(
            day,
            NoteIn(market_view="我的观点", next_plan=None),
            user=user,
            session=session,
        )
        assert out.market_view == "我的观点"
        rows = session.scalars(select(ReviewNote)).all()
        assert len(rows) == 1
        assert rows[0].market_view == "我的观点"


# ------------------------------------------------------- #3 行业宽度的目标日


def test_industry_breadth_target_skips_partial_day(engine):
    day_full = date(2026, 8, 3)
    day_partial = date(2026, 8, 4)
    with Session(engine) as db:
        db.add_all(
            [
                TradeCalendar(trade_date=day_full),
                TradeCalendar(trade_date=day_partial),
                # 股票池 10 只（覆盖面闸的分母）
                *[StockUniverse(code=f"60000{i}") for i in range(10)],
                # 前一天采全：10 只有效日线
                *[
                    StockDaily(
                        code=f"60000{i}", trade_date=day_full, close=10.0, pct_chg=1.0
                    )
                    for i in range(10)
                ],
                # 当天只采到 3 只（+ 一条停牌残行，不算采到）
                *[
                    StockDaily(
                        code=f"60000{i}",
                        trade_date=day_partial,
                        close=10.0,
                        pct_chg=1.0,
                    )
                    for i in range(3)
                ],
                StockDaily(code="600099", trade_date=day_partial, close=10.0),
            ]
        )
        db.commit()
        assert market_api._valid_stock_rows(db, day_partial) == 3
        target, rows = market_api._latest_covered_stock_day(db)
        assert target == day_full  # 不拿只有 3/10 的「半天」算全市场占比
        assert rows == 10


# --------------------------------------------------------- #4 停牌残行过滤


def _add_residual_rows(db: Session) -> None:
    db.add_all(
        [
            StockDaily(
                code="600519", trade_date=date(2026, 8, 3), close=100.0, pct_chg=1.0
            ),
            # iFinD 停牌日残行：只有收盘价、其余全空
            StockDaily(code="600519", trade_date=date(2026, 8, 4), close=100.0),
        ]
    )
    db.commit()


def test_watchlist_latest_quote_skips_residual_row(engine):
    with Session(engine) as db:
        _add_residual_rows(db)
        quotes = watchlist_api._latest_quotes(db, ["600519"])
        assert quotes["600519"].trade_date == date(2026, 8, 3)
        assert quotes["600519"].pct_chg == 1.0


def test_profile_latest_quote_skips_residual_row(engine, monkeypatch):
    # 个股页的阶段判定读真实库（`load_phase` 内部自己开 session），这里只钉取数口径
    monkeypatch.setattr(stock_api, "load_phase", lambda session, code: None)
    with Session(engine) as db:
        _add_residual_rows(db)
        out = stock_api.profile("600519", user=db.get(AppUser, 1), session=db)
        assert out.latest is not None
        assert out.latest.trade_date == date(2026, 8, 3)
        assert out.latest.pct_chg == 1.0


# ------------------------------------------------------------- #5 ETF 基准日


def test_etf_covered_day_skips_fragment_and_nontrading_snapshots(engine, monkeypatch):
    monkeypatch.setattr(etf_flow, "MIN_ROWS", 3)
    covered = date(2026, 9, 18)
    fragment = date(2026, 9, 21)
    nontrading = date(2026, 9, 20)  # 周日：货基按自然日给快照，行数也可能够
    with Session(engine) as db:
        db.add_all(
            [
                TradeCalendar(trade_date=covered),
                TradeCalendar(trade_date=fragment),
                *[
                    EtfShare(
                        trade_date=covered, code=f"51000{i}", shares=100.0, close=1.0
                    )
                    for i in range(3)
                ],
                # 碎片日：只有 1 行
                EtfShare(trade_date=fragment, code="510000", shares=100.0, close=1.0),
                *[
                    EtfShare(
                        trade_date=nontrading, code=f"51000{i}", shares=100.0, close=1.0
                    )
                    for i in range(3)
                ],
            ]
        )
        db.commit()
        # 9-21 是碎片、9-20 不在交易日历里 → 基准日要退到 9-18
        assert etf_flow.prev_covered_day(db, fragment) == covered
        assert etf_flow.prev_covered_day(db, date(2026, 9, 22)) == covered
        assert etf_flow.latest_covered_day(db, fragment) == covered
        # 当日快照覆盖不足时不给「净申赎合计」（None，不是 0）
        assert funds_api._etf_net_inflow(db, fragment, covered) is None
