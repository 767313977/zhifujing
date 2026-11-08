"""2026-11-08 第二轮复审整改的回归测试（P1-NEW-01 ~ P2-NEW-05）。

钉住六类边界：

1. 计划回填不能改写**已平仓**交易的历史（成本 / 日期 / 收益），也不能越过已有做 T 的边界；
2. 回填中途失败时，计划状态与交易日志一起回滚；
3. 「转持仓」撞上已平仓日志时返回 409，且计划与交易都不变；
4. 两个独立 Session 并发执行同一计划，只留一笔日志、不抛未处理异常；
5. 500 笔日志上限在并发新增时是**原子**的，不会突破；
6. 删除计划只解除关联，不删除交易 / 做 T / 收益历史。
"""

from datetime import date
from threading import Barrier, Thread

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.api import auth as auth_api
from app.api import plans as plans_api
from app.api import positions as positions_api
from app.models import (
    AppUser,
    Base,
    StockBasic,
    StockDaily,
    TradeCalendar,
    TradeJournal,
    TradePlan,
    TradeT,
)
from app.schemas import PlanExecuteIn, TradePlanIn
from app.services import auth, trade_journal, trade_plan
from app.services.auth import COOKIE_NAME

D1 = date(2026, 11, 2)
D2 = date(2026, 11, 3)
D3 = date(2026, 11, 4)
D4 = date(2026, 11, 5)
D5 = date(2026, 11, 6)


def _engine(tmp_path, name: str):
    """独立文件库。`check_same_thread=False` 给并发用例（多线程各自开 Session）。"""
    return create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )


def _seed(engine) -> tuple[int, int]:
    """建用户 / 股票 / 交易日历 / 日线 / 一条计划，返回 `(user_id, plan_id)`。"""
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([TradeCalendar(trade_date=day) for day in (D1, D2, D3, D4, D5)])
        session.add(StockBasic(code="300001", name="测试A"))
        session.add_all(
            [
                StockDaily(trade_date=D1, code="300001", close=10.0, pct_chg=0.0),
                StockDaily(trade_date=D2, code="300001", close=10.2, pct_chg=2.0),
                StockDaily(trade_date=D3, code="300001", close=10.4, pct_chg=1.96),
                StockDaily(trade_date=D4, code="300001", close=10.6, pct_chg=1.92),
                StockDaily(trade_date=D5, code="300001", close=10.8, pct_chg=1.89),
            ]
        )
        user = AppUser(username="u1", password_hash="x")
        session.add(user)
        session.flush()
        plan = TradePlan(
            user_id=user.id,
            code="300001",
            name="测试A",
            plan_date=D5,
            buy_price=10.0,
            stop_price=9.0,
            source="volume_surge",
        )
        session.add(plan)
        session.commit()
        return user.id, plan.id


@pytest.fixture()
def db(tmp_path):
    engine = _engine(tmp_path, "round2.db")
    user_id, plan_id = _seed(engine)
    with Session(engine) as session:
        yield session, session.get(AppUser, user_id), session.get(TradePlan, plan_id), engine


def _journal(
    session: Session,
    user_id: int,
    *,
    entry: date,
    exit_day: date | None = None,
    plan_id: int | None = None,
    entry_price: float = 10.0,
) -> TradeJournal:
    journal = TradeJournal(
        user_id=user_id,
        code="300001",
        name="测试A",
        entry_date=entry,
        entry_price=entry_price,
        exit_date=exit_day,
        exit_price=11.0 if exit_day is not None else None,
        plan_id=plan_id,
    )
    session.add(journal)
    session.commit()
    return journal


# ---------------------------------------------------------------- P1-NEW-01 回填保护


def test_closed_journal_backfill_is_rejected_without_history_change(db):
    """已平仓后再回填：409，且买入日 / 成本 / 收益原样不动。"""
    session, user, plan, engine = db
    journal = _journal(session, user.id, entry=D3, exit_day=D5, plan_id=plan.id)
    before = trade_journal.load_enriched(session, user.id, journal.id)[0]

    with pytest.raises(HTTPException) as info:
        plans_api.execute_plan(
            plan.id,
            PlanExecuteIn(actual_price=99.0, entry_date=D4),
            session,
            user,
        )
    assert info.value.status_code == 409
    session.rollback()

    with Session(engine) as fresh:
        row = fresh.get(TradeJournal, journal.id)
        assert row.entry_date == D3
        assert row.entry_price == 10.0
        assert row.exit_date == D5
        assert row.exit_price == 11.0
        plan_row = fresh.get(TradePlan, plan.id)
        assert plan_row.status != "executed"
        assert plan_row.actual_price is None
        after = trade_journal.load_enriched(fresh, user.id, journal.id)[0]
    assert after["return_pct"] == before["return_pct"]


def test_backfill_entry_date_cannot_pass_existing_exit(db):
    """买入日不得晚于已有卖出日：明确 400，且不落库。"""
    session, user, plan, engine = db
    journal = _journal(session, user.id, entry=D3, exit_day=D4, plan_id=plan.id)

    with pytest.raises(HTTPException) as info:
        plans_api.execute_plan(
            plan.id,
            PlanExecuteIn(actual_price=10.5, entry_date=D5),
            session,
            user,
        )
    assert info.value.status_code == 400
    assert "卖出日" in info.value.detail
    session.rollback()
    with Session(engine) as fresh:
        assert fresh.get(TradeJournal, journal.id).entry_date == D3


def test_backfill_must_respect_existing_t_bounds(db):
    """未平仓但有做 T：把买入日改到做 T 当天/之后必须被拒（400）。"""
    session, user, plan, engine = db
    journal = _journal(session, user.id, entry=D1, plan_id=plan.id)
    session.add(
        TradeT(
            user_id=user.id,
            trade_id=journal.id,
            trade_date=D3,
            direction="long",
            buy_price=10.3,
            sell_price=10.5,
            ratio_pct=33.0,
            trigger="other",
        )
    )
    session.commit()

    with pytest.raises(HTTPException) as info:
        plans_api.execute_plan(
            plan.id,
            PlanExecuteIn(actual_price=10.5, entry_date=D3),
            session,
            user,
        )
    assert info.value.status_code == 400
    assert "做 T" in info.value.detail
    session.rollback()
    with Session(engine) as fresh:
        assert fresh.get(TradeJournal, journal.id).entry_date == D1
        assert fresh.get(TradePlan, plan.id).status != "executed"


def test_backfill_with_unchanged_entry_date_still_works(db):
    """买入日与做 T 不冲突时照常更新，不误伤。"""
    session, user, plan, _ = db
    journal = _journal(session, user.id, entry=D1, plan_id=plan.id)
    session.add(
        TradeT(
            user_id=user.id,
            trade_id=journal.id,
            trade_date=D3,
            direction="long",
            buy_price=10.3,
            sell_price=10.5,
            ratio_pct=33.0,
            trigger="other",
        )
    )
    session.commit()

    out = plans_api.execute_plan(
        plan.id,
        PlanExecuteIn(actual_price=10.5, entry_date=D1),
        session,
        user,
    )
    assert out.journal_id == journal.id
    session.refresh(journal)
    assert journal.entry_date == D1
    assert journal.entry_price == 10.5
    assert len(list(session.scalars(select(TradeT)))) == 1


def test_backfill_failure_rolls_back_plan_and_journal(db, monkeypatch):
    """事务中途异常：计划状态与交易日志一起回滚。"""
    session, user, plan, engine = db
    journal = _journal(session, user.id, entry=D3, plan_id=plan.id)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("模拟中途失败")

    monkeypatch.setattr(plans_api, "_apply_journal", _boom)
    with pytest.raises(RuntimeError):
        plans_api.execute_plan(
            plan.id,
            PlanExecuteIn(actual_price=10.9, entry_date=D4),
            session,
            user,
        )

    with Session(engine) as fresh:
        row = fresh.get(TradeJournal, journal.id)
        assert row.entry_date == D3
        assert row.entry_price == 10.0
        plan_row = fresh.get(TradePlan, plan.id)
        assert plan_row.actual_price is None
        assert plan_row.status != "executed"


# ---------------------------------------------------------------- P2-NEW-03 已平仓转持仓


def test_from_plan_returns_409_for_closed_journal_and_keeps_data(db):
    """已平仓的计划再转持仓：409，计划与交易都不得修改。"""
    session, user, plan, engine = db
    journal = _journal(session, user.id, entry=D3, exit_day=D5, plan_id=plan.id)
    session.refresh(plan)
    plan_snapshot = (plan.status, plan.actual_price, plan.followed_plan)

    with pytest.raises(HTTPException) as info:
        positions_api.create_from_plan(plan.id, session, user)
    assert info.value.status_code == 409
    session.rollback()

    with Session(engine) as fresh:
        plan_row = fresh.get(TradePlan, plan.id)
        assert (plan_row.status, plan_row.actual_price, plan_row.followed_plan) == plan_snapshot
        assert fresh.get(TradeJournal, journal.id).exit_date == D5
        assert fresh.scalar(
            select(func.count()).select_from(TradeJournal).where(TradeJournal.plan_id == plan.id)
        ) == 1


def test_manual_position_with_closed_plan_journal_returns_409(db):
    """手工建持仓时挂到已平仓的计划上，也按 409 处理。"""
    session, user, plan, _ = db
    _journal(session, user.id, entry=D3, exit_day=D5, plan_id=plan.id)

    with pytest.raises(HTTPException) as info:
        positions_api.create_position(
            positions_api.PositionIn(
                code="300001", entry_date=D5, entry_price=10.5, plan_id=plan.id
            ),
            session,
            user,
        )
    assert info.value.status_code == 409


# ---------------------------------------------------------------- 并发执行同一计划


def test_two_sessions_execute_same_plan_keeps_single_journal(tmp_path):
    engine = _engine(tmp_path, "concurrent-plan.db")
    user_id, plan_id = _seed(engine)
    barrier = Barrier(2)
    results: list[tuple[str, object]] = []

    def worker() -> None:
        with Session(engine) as session:
            user = session.get(AppUser, user_id)
            barrier.wait()
            try:
                out = plans_api.execute_plan(
                    plan_id, PlanExecuteIn(actual_price=10.2), session, user
                )
                results.append(("ok", out.journal_id))
            except BaseException as exc:  # noqa: BLE001 - 测试要看到所有形态
                results.append(("err", exc))

    threads = [Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    with Session(engine) as session:
        rows = list(
            session.scalars(select(TradeJournal).where(TradeJournal.plan_id == plan_id))
        )
    assert len(rows) == 1
    ok_ids = {value for kind, value in results if kind == "ok"}
    assert ok_ids  # 至少一个请求成功
    assert ok_ids == {rows[0].id}
    for kind, value in results:
        if kind == "err":
            # 允许明确的业务冲突，不允许 IntegrityError / OperationalError 漏到用户面前
            assert isinstance(value, HTTPException)
            assert value.status_code in (400, 409)


# ---------------------------------------------------------------- P2-NEW-04 上限原子性


def test_journal_limit_is_atomic_under_concurrency(tmp_path):
    engine = _engine(tmp_path, "limit.db")
    user_id, _ = _seed(engine)
    with Session(engine) as session:
        session.add_all(
            [
                TradeJournal(
                    user_id=user_id,
                    code="300001",
                    entry_date=D5,
                    entry_price=10.0,
                )
                for _ in range(trade_journal.MAX_ENTRIES - 1)
            ]
        )
        session.commit()

    barrier = Barrier(2)
    results: list[str] = []

    def worker(code: str) -> None:
        with Session(engine) as session:
            barrier.wait()
            try:
                trade_journal.insert_trade(
                    session,
                    user_id,
                    code=code,
                    entry_date=D5,
                    entry_price=10.0,
                    reason_type="other",
                )
                session.commit()
                results.append("ok")
            except ValueError:
                session.rollback()
                results.append("limit")

    threads = [Thread(target=worker, args=(code,)) for code in ("300001", "300002")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    with Session(engine) as session:
        total = session.scalar(
            select(func.count()).select_from(TradeJournal).where(TradeJournal.user_id == user_id)
        )
    assert sorted(results) == ["limit", "ok"]
    assert total == trade_journal.MAX_ENTRIES


def test_repeat_existing_journal_does_not_consume_limit(db, monkeypatch):
    """已是上限时，重复回填 / 重复转持仓（命中已有日志）不能被额度挡住。"""
    session, user, plan, _ = db
    journal = _journal(session, user.id, entry=D3, plan_id=plan.id)
    monkeypatch.setattr(trade_journal, "MAX_ENTRIES", 1)

    out = plans_api.execute_plan(
        plan.id, PlanExecuteIn(actual_price=10.6, entry_date=D4), session, user
    )
    assert out.journal_id == journal.id
    again = positions_api.create_from_plan(plan.id, session, user)
    assert again.id == journal.id


def test_plan_limit_is_enforced_on_atomic_insert(db, monkeypatch):
    """计划上限同样走原子插入：满额时新增返回明确错误。"""
    session, user, _plan, _ = db
    monkeypatch.setattr(trade_plan, "MAX_PLANS", 1)
    with pytest.raises(ValueError, match="交易计划最多"):
        trade_plan.insert_plan(
            session,
            user.id,
            {
                "code": "300001",
                "plan_date": D5,
                "buy_price": 10.0,
                "stop_price": 9.0,
            },
        )


def test_create_plan_still_works_through_atomic_insert(db):
    """改成原子插入后，普通「新增计划」路径本身要照常可用。"""
    session, user, _plan, _ = db
    row = plans_api.create_plan(
        TradePlanIn(
            code="300001",
            name="测试A",
            plan_date=D4,
            buy_price=10.2,
            stop_price=9.2,
            note="新增",
        ),
        session,
        user,
    )
    assert row.plan_date == D4
    assert row.buy_price == 10.2
    assert session.scalar(select(func.count()).select_from(TradePlan)) == 2


def test_create_plan_over_limit_returns_400(db, monkeypatch):
    """API 入口把原子的上限错误翻译成 400，而不是 500。"""
    session, user, _plan, _ = db
    monkeypatch.setattr(trade_plan, "MAX_PLANS", 1)
    with pytest.raises(HTTPException) as info:
        plans_api.create_plan(
            TradePlanIn(
                code="300001",
                name="测试A",
                plan_date=D4,
                buy_price=10.2,
                stop_price=9.2,
            ),
            session,
            user,
        )
    assert info.value.status_code == 400
    assert "交易计划最多" in info.value.detail


# ---------------------------------------------------------------- P2-NEW-05 删除计划


def test_delete_plan_detaches_journal_and_keeps_history(db):
    """删计划只解除 plan_id 关联：交易、做 T、收益历史全部保留。"""
    session, user, plan, engine = db
    journal = _journal(session, user.id, entry=D1, exit_day=D5, plan_id=plan.id)
    session.add(
        TradeT(
            user_id=user.id,
            trade_id=journal.id,
            trade_date=D3,
            leg2_date=D4,
            direction="long",
            buy_price=10.3,
            sell_price=10.5,
            ratio_pct=33.0,
            trigger="other",
        )
    )
    session.commit()

    assert plans_api.delete_plan(plan.id, session, user)["ok"] is True

    with Session(engine) as fresh:
        assert fresh.get(TradePlan, plan.id) is None
        row = fresh.get(TradeJournal, journal.id)
        assert row is not None
        assert row.plan_id is None
        assert row.exit_date == D5
        assert fresh.scalar(select(func.count()).select_from(TradeT)) == 1
        # 不留悬空 plan_id
        dangling = fresh.scalar(
            select(func.count())
            .select_from(TradeJournal)
            .where(
                TradeJournal.plan_id.is_not(None),
                ~TradeJournal.plan_id.in_(select(TradePlan.id)),
            )
        )
        assert dangling == 0


# ---------------------------------------------------------------- 登出（服务端撤销）


def test_logout_revokes_server_session(db):
    session, user, _plan, _ = db
    token = auth.create_session(session, user, "pytest")
    session.commit()
    assert auth.resolve_session(session, token) is not None

    request = auth_api.Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/logout",
            "headers": [
                (b"host", b"127.0.0.1"),
                (b"cookie", f"{COOKIE_NAME}={token}".encode()),
            ],
            "client": ("10.9.9.9", 1234),
            "scheme": "http",
            "query_string": b"",
        }
    )
    response = auth_api.Response()
    assert auth_api.logout(request, response, session)["ok"] is True
    assert auth.resolve_session(session, token) is None
    assert COOKIE_NAME in response.headers["set-cookie"]
    # 重复登出无害（幂等）
    assert auth_api.logout(request, response, session)["ok"] is True
