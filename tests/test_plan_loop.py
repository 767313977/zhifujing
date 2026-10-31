"""计划闭环（FEAT-PLAN-LOOP）：名单生计划、状态同步、执行回填、区间统计。

口径（与 service 注释一一对应）：
- 一键生成不覆盖用户改过的计划（重复点击返回原计划）；
- 计划状态落库并自动同步，手动改（取消/已执行）优先；
- 回填实际价会写一笔关联日志，重复回填只更新同一笔；
- 统计分母排除「仅观察」，没有平仓日志时盈亏给 null 而不是 0。
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import (
    AppUser,
    Base,
    PatternHit,
    StockBasic,
    TradeCalendar,
    TradeJournal,
    TradePlan,
)
from app.services import trade_plan
from app.timeutil import today_cn

TODAY = today_cn()


def _weekdays_around(day: date, before: int, after: int) -> list[date]:
    """以 `day` 为界，取不重复的工作日（模拟交易日历）。"""
    days: list[date] = []
    cursor = day
    while len([item for item in days if item <= day]) < before:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    cursor = day
    while len([item for item in days if item > day]) < after:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days.append(cursor)
    return sorted(set(days))


CALENDAR = _weekdays_around(TODAY, before=8, after=5)
#: 信号日 = 今天之前最近的一个交易日；它的下一个交易日就是执行日
SIGNAL = [day for day in CALENDAR if day < TODAY][-1]


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'plan-loop.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([TradeCalendar(trade_date=day) for day in CALENDAR])
        session.add_all(
            [
                StockBasic(code="300001", name="测试A"),
                StockBasic(code="300002", name="测试B"),
            ]
        )
        session.add_all(
            [
                PatternHit(
                    trade_date=SIGNAL,
                    code="300001",
                    pattern="wudao_start",
                    name="测试A",
                    score=80.0,
                    close=10.5,
                    key_levels={"breakout": 10.8, "support": 9.9},
                    detail={},
                ),
                PatternHit(
                    trade_date=SIGNAL,
                    code="300002",
                    pattern="wudao_sample",
                    name="测试B",
                    score=70.0,
                    close=8.2,
                    key_levels={"watch_high": 8.5},
                    detail={},
                ),
            ]
        )
        user = AppUser(username="u1", password_hash="x")
        other = AppUser(username="u2", password_hash="x")
        session.add_all([user, other])
        session.commit()
        yield session, user.id, other.id


def test_from_signal_uses_levels_and_next_trade_day(db):
    session, user_id, _ = db
    row, created = trade_plan.create_from_signal(
        session, user_id, "300001", "wudao_start", signal_date=SIGNAL
    )
    assert created is True
    assert row["plan_date"] > SIGNAL
    assert row["buy_price"] == 10.8
    assert row["stop_price"] == 9.9
    assert row["signal_date"] == SIGNAL
    assert row["ref_price"] == 10.5
    assert row["source_name"] == "明天预案"


def test_from_signal_falls_back_stop_and_does_not_overwrite(db):
    session, user_id, _ = db
    row, created = trade_plan.create_from_signal(
        session, user_id, "300002", "wudao_sample", signal_date=SIGNAL
    )
    assert created is True
    assert row["buy_price"] == 8.5
    # 没有 support/wash_low → 按触发价 × 0.95 兜底
    assert row["stop_price"] == pytest.approx(8.07, abs=0.01)
    assert "兜底" in (row["note"] or "")

    plan = session.get(TradePlan, row["id"])
    plan.buy_price = 8.6
    session.commit()

    again, created_again = trade_plan.create_from_signal(
        session, user_id, "300002", "wudao_sample", signal_date=SIGNAL
    )
    assert created_again is False
    assert again["buy_price"] == 8.6  # 用户改过的价位没被冲掉


def test_status_sync_and_manual_priority(db):
    session, user_id, _ = db
    today = today_cn()
    past = TradePlan(
        user_id=user_id,
        code="300001",
        plan_date=today - timedelta(days=5),
        buy_price=10.0,
        stop_price=9.0,
    )
    future = TradePlan(
        user_id=user_id,
        code="300002",
        plan_date=today + timedelta(days=3),
        buy_price=8.0,
        stop_price=7.0,
    )
    session.add_all([past, future])
    session.commit()

    rows = {row["code"]: row for row in trade_plan.load(session, user_id)}
    assert rows["300001"]["plan_status"] == "expired"
    assert rows["300002"]["plan_status"] == "observe"

    past.status = "cancelled"
    past.status_source = "manual"
    session.commit()
    rows = {row["code"]: row for row in trade_plan.load(session, user_id)}
    assert rows["300001"]["plan_status"] == "cancelled"  # 手动优先，不被改回 expired


def test_execute_creates_one_linked_journal(db):
    session, user_id, _ = db
    plan = TradePlan(
        user_id=user_id,
        code="300001",
        name="测试A",
        plan_date=today_cn(),
        buy_price=10.0,
        stop_price=9.0,
        source="wudao_start",
        status_source="auto",
    )
    session.add(plan)
    session.commit()

    from app.api import plans as plans_api
    from app.schemas import PlanExecuteIn

    user = session.get(AppUser, user_id)
    first = plans_api.execute_plan(
        plan.id,
        PlanExecuteIn(actual_price=10.2, followed_plan=True, deviation=None),
        session,
        user,
    )
    second = plans_api.execute_plan(
        plan.id,
        PlanExecuteIn(
            actual_price=10.3, followed_plan=False, deviation="追高"
        ),
        session,
        user,
    )
    assert first.journal_id == second.journal_id
    journals = list(
        session.scalars(select(TradeJournal).where(TradeJournal.plan_id == plan.id))
    )
    assert len(journals) == 1
    assert journals[0].entry_price == 10.3
    assert journals[0].reason_text == "追高"
    refreshed = session.get(TradePlan, plan.id)
    assert refreshed.status == "executed"
    assert refreshed.status_source == "manual"


def test_stats_excludes_observe_and_reports_rates(db):
    session, user_id, _ = db
    today = today_cn()
    session.add_all(
        [
            TradePlan(
                user_id=user_id,
                code="300001",
                plan_date=today - timedelta(days=2),
                buy_price=10.0,
                stop_price=9.0,
                actual_price=10.1,
                followed_plan=True,
                status="executed",
                status_source="manual",
                deviation="追高",
            ),
            TradePlan(
                user_id=user_id,
                code="300002",
                plan_date=today - timedelta(days=2),
                buy_price=8.0,
                stop_price=7.0,
                observe_only=True,
            ),
        ]
    )
    session.commit()
    plan = session.scalar(
        select(TradePlan).where(
            TradePlan.user_id == user_id, TradePlan.code == "300001"
        )
    )
    session.add(
        TradeJournal(
            user_id=user_id,
            code="300001",
            entry_date=today - timedelta(days=2),
            entry_price=10.0,
            exit_date=today - timedelta(days=1),
            exit_price=11.0,
            plan_id=plan.id,
        )
    )
    session.commit()

    data = trade_plan.stats(
        session, user_id, today - timedelta(days=10), today
    )
    assert data["plans"] == 2
    assert data["observe"] == 1
    assert data["observable"] == 1
    assert data["executed"] == 1
    assert data["execution_rate"] == 100.0
    assert data["follow_rate"] == 100.0
    assert data["closed"] == 1
    assert data["win_rate"] == 100.0
    assert data["avg_return_pct"] == 10.0
    assert data["by_deviation"] == {"追高": 1}
    assert data["note"] is None


def test_stats_without_closed_journal_gives_null_not_zero(db):
    session, user_id, _ = db
    today = today_cn()
    session.add(
        TradePlan(
            user_id=user_id,
            code="300001",
            plan_date=today,
            buy_price=10.0,
            stop_price=9.0,
        )
    )
    session.commit()
    data = trade_plan.stats(session, user_id, today, today)
    assert data["execution_rate"] == 0.0
    assert data["win_rate"] is None
    assert data["avg_return_pct"] is None
    assert data["note"]
