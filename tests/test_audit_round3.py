"""2026-11-08 第三轮复审整改回归（P1-C / P2-C / P2-D）。

钉住三类边界：

1. 登录成功时必须在同一事务里撤销请求携带的旧会话，否则「注销失败后直接重新登录」
   会在服务端留下两条有效会话；
2. 500 笔日志的**最后一个额度**被同一计划的并发请求抢走时，后来者应按业务唯一键
   重查并返回同一笔，而不是误报「额度已满」；
3. 从名单生成计划在并发同键时撞唯一索引，必须回滚后按业务键重查，不能抛未处理 500。
"""

from datetime import date
from threading import Barrier, Thread

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response

from app.api import auth as auth_api
from app.api import plans as plans_api
from app.api import positions as positions_api
from app.models import (
    AppUser,
    Base,
    PatternHit,
    StockBasic,
    StockDaily,
    TradeCalendar,
    TradeJournal,
    TradePlan,
    UserSession,
)
from app.schemas import LoginIn, PlanExecuteIn
from app.services import auth, trade_journal, trade_plan

D1 = date(2026, 11, 2)
D2 = date(2026, 11, 3)
D3 = date(2026, 11, 4)
D4 = date(2026, 11, 5)
D5 = date(2026, 11, 6)

PASSWORD = "Test12345!"


def _engine(tmp_path, name: str):
    return create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )


def _seed(engine) -> tuple[int, int]:
    """用户 / 股票 / 日历 / 日线 / 一条计划（计划日 D5）。"""
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
        user = AppUser(username="u1", password_hash=auth.hash_password(PASSWORD))
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
    engine = _engine(tmp_path, "round3.db")
    user_id, plan_id = _seed(engine)
    with Session(engine) as session:
        yield session, session.get(AppUser, user_id), session.get(TradePlan, plan_id)


# ---------------------------------------------------------------- P1-C 登录撤销旧会话


def _request(cookie: str | None = None) -> Request:
    headers = [(b"host", b"127.0.0.1")]
    if cookie is not None:
        headers.append((b"cookie", f"{auth.COOKIE_NAME}={cookie}".encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "headers": headers,
            "client": ("10.9.9.9", 1234),
            "scheme": "http",
            "query_string": b"",
        }
    )


def _relax_limits(monkeypatch) -> None:
    monkeypatch.setattr(auth, "guard_rate", lambda ip: None)
    monkeypatch.setattr(auth, "guard_user_rate", lambda username, ip: None)
    monkeypatch.setattr(auth.login_limiter, "reset", lambda key: None)
    monkeypatch.setattr(auth.login_user_limiter, "reset", lambda key: None)


def _cookie_token(header: str) -> str:
    return header.split(";")[0].split("=", 1)[1]


def test_login_revokes_presented_old_session(db, monkeypatch):
    """注销请求失败后直接重新登录：旧会话必须被同一事务撤销，不能留两条。"""
    session, user, _ = db
    old_token = auth.create_session(session, user, "old")
    session.commit()
    assert auth.resolve_session(session, old_token) is not None

    _relax_limits(monkeypatch)
    response = Response()
    out = auth_api.login(
        LoginIn(username="u1", password=PASSWORD),
        _request(old_token),
        response,
        session,
    )
    assert out.username == "u1"

    session.expire_all()
    assert auth.resolve_session(session, old_token) is None
    rows = list(session.scalars(select(UserSession)))
    assert len(rows) == 1  # 只剩这次登录新建立的会话
    new_token = _cookie_token(response.headers["set-cookie"])
    assert auth.resolve_session(session, new_token) is not None


def test_login_failure_keeps_presented_old_session(db, monkeypatch):
    """密码错时不能顺手把旧会话删掉：用户还没登录成功。"""
    session, user, _ = db
    old_token = auth.create_session(session, user, "old")
    session.commit()

    _relax_limits(monkeypatch)
    monkeypatch.setattr(auth.login_limiter, "fail", lambda key: None)
    monkeypatch.setattr(auth.login_user_limiter, "fail", lambda key: None)
    with pytest.raises(HTTPException) as info:
        auth_api.login(
            LoginIn(username="u1", password="wrong-password"),
            _request(old_token),
            Response(),
            session,
        )
    assert info.value.status_code == 401
    session.expire_all()
    assert auth.resolve_session(session, old_token) is not None


def test_logout_old_session_does_not_revoke_newer_one(db):
    """注销旧 token 不能误伤之后建立的新会话（交错完成的底层保证）。"""
    session, user, _ = db
    old_token = auth.create_session(session, user, "old")
    new_token = auth.create_session(session, user, "new")
    session.commit()

    auth.delete_session(session, old_token)
    session.expire_all()
    assert auth.resolve_session(session, old_token) is None
    assert auth.resolve_session(session, new_token) is not None


# ---------------------------------------------------------------- P2-C 额度最后一位


def test_execute_plan_last_slot_returns_same_journal(tmp_path):
    """499 笔 + 两个并发请求操作同一计划：只留 1 条日志、返回同一 id，不报额度满。"""
    engine = _engine(tmp_path, "p2c-execute.db")
    user_id, plan_id = _seed(engine)
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
        thread.join(timeout=40)
        assert not thread.is_alive()

    assert [kind for kind, _ in results] == ["ok", "ok"]
    ids = {value for kind, value in results if kind == "ok"}
    assert len(ids) == 1

    with Session(engine) as session:
        linked = list(
            session.scalars(select(TradeJournal).where(TradeJournal.plan_id == plan_id))
        )
        total = session.scalar(
            select(func.count())
            .select_from(TradeJournal)
            .where(TradeJournal.user_id == user_id)
        )
    assert len(linked) == 1
    assert ids == {linked[0].id}
    assert total == trade_journal.MAX_ENTRIES


def test_from_plan_last_slot_returns_same_position(tmp_path):
    """同一计划转持仓：499→500 的最后一位被抢走时，后来者仍返回同一笔持仓。"""
    engine = _engine(tmp_path, "p2c-from-plan.db")
    user_id, plan_id = _seed(engine)
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
    results: list[tuple[str, object]] = []

    def worker() -> None:
        with Session(engine) as session:
            user = session.get(AppUser, user_id)
            barrier.wait()
            try:
                row = positions_api.create_from_plan(plan_id, session, user)
                results.append(("ok", row.id))
            except BaseException as exc:  # noqa: BLE001
                results.append(("err", exc))

    threads = [Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=40)
        assert not thread.is_alive()

    assert [kind for kind, _ in results] == ["ok", "ok"]
    ids = {value for kind, value in results if kind == "ok"}
    assert len(ids) == 1
    with Session(engine) as session:
        total = session.scalar(
            select(func.count())
            .select_from(TradeJournal)
            .where(TradeJournal.user_id == user_id)
        )
    assert total == trade_journal.MAX_ENTRIES


def test_different_new_record_at_limit_still_rejected(tmp_path):
    """额度满了、目标又确实是另一条新记录：仍要给明确的上限错误。"""
    engine = _engine(tmp_path, "p2c-different.db")
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
                for _ in range(trade_journal.MAX_ENTRIES)
            ]
        )
        session.commit()
        with pytest.raises(ValueError, match="交易日志最多"):
            trade_journal.insert_trade(
                session,
                user_id,
                code="300001",
                entry_date=D5,
                entry_price=10.0,
            )


# ---------------------------------------------------------------- P2-D 从名单生成计划


def _seed_signal(engine, *, plans: int = 0) -> tuple[int, date, date]:
    """用户 + 日历 + 形态命中 + 可选的既有计划。返回 `(user_id, signal, target)`。"""
    Base.metadata.create_all(engine)
    signal = D4
    target = D5
    with Session(engine) as session:
        session.add_all(
            [TradeCalendar(trade_date=day) for day in (D1, D2, D3, D4, D5)]
        )
        session.add(StockBasic(code="300001", name="测试A"))
        session.add(
            PatternHit(
                trade_date=signal,
                code="300001",
                pattern="volume_surge",
                name="测试A",
                score=80.0,
                close=10.5,
                key_levels={"breakout": 10.8, "support": 9.9},
                detail={},
            )
        )
        user = AppUser(username="u1", password_hash="x")
        session.add(user)
        session.flush()
        session.add_all(
            [
                TradePlan(
                    user_id=user.id,
                    code=f"{500000 + index}",
                    plan_date=D1,
                    buy_price=1.0,
                    stop_price=0.5,
                )
                for index in range(plans)
            ]
        )
        session.commit()
        return user.id, signal, target


def _run_concurrent_from_signal(engine, user_id: int, signal: date, overwrite: bool):
    barrier = Barrier(2)
    results: list[tuple[str, object]] = []

    def worker() -> None:
        with Session(engine) as session:
            barrier.wait()
            try:
                row, created = trade_plan.create_from_signal(
                    session,
                    user_id,
                    "300001",
                    "volume_surge",
                    signal_date=signal,
                    overwrite=overwrite,
                )
                results.append(("ok", (created, row["id"])))
            except BaseException as exc:  # noqa: BLE001
                results.append(("err", exc))

    threads = [Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=40)
        assert not thread.is_alive()
    return results


def test_from_signal_concurrent_same_key_single_plan(tmp_path):
    """两个独立 Session 并发生成同一计划：只留一条，后者返回已有记录。"""
    engine = _engine(tmp_path, "p2d-concurrent.db")
    user_id, signal, target = _seed_signal(engine)

    results = _run_concurrent_from_signal(engine, user_id, signal, overwrite=False)
    assert [kind for kind, _ in results] == ["ok", "ok"]
    payloads = [value for _, value in results]
    assert len({created for created, _ in payloads}) == 2  # 一个 True、一个 False
    ids = {plan_id for _, plan_id in payloads}
    assert len(ids) == 1

    with Session(engine) as session:
        rows = list(
            session.scalars(
                select(TradePlan).where(
                    TradePlan.user_id == user_id,
                    TradePlan.code == "300001",
                    TradePlan.plan_date == target,
                )
            )
        )
    assert len(rows) == 1


def test_from_signal_concurrent_last_plan_slot_single_plan(tmp_path):
    """199→200 的最后一位被同一业务键抢走：不能报额度满，也不能插第二条。"""
    engine = _engine(tmp_path, "p2d-last-slot.db")
    user_id, signal, target = _seed_signal(engine, plans=trade_plan.MAX_PLANS - 1)

    results = _run_concurrent_from_signal(engine, user_id, signal, overwrite=False)
    assert [kind for kind, _ in results] == ["ok", "ok"]
    ids = {plan_id for _, (_, plan_id) in results}
    assert len(ids) == 1

    with Session(engine) as session:
        total = session.scalar(
            select(func.count()).select_from(TradePlan).where(TradePlan.user_id == user_id)
        )
        linked = session.scalar(
            select(func.count())
            .select_from(TradePlan)
            .where(
                TradePlan.user_id == user_id,
                TradePlan.code == "300001",
                TradePlan.plan_date == target,
            )
        )
    assert total == trade_plan.MAX_PLANS
    assert linked == 1


def test_from_signal_overwrite_false_keeps_manual_edit(tmp_path):
    engine = _engine(tmp_path, "p2d-keep.db")
    user_id, signal, target = _seed_signal(engine)
    with Session(engine) as session:
        plan, created = trade_plan.create_from_signal(
            session, user_id, "300001", "volume_surge", signal_date=signal
        )
        assert created is True
        row = session.get(TradePlan, plan["id"])
        row.buy_price = 99.0
        session.commit()

        again, created_again = trade_plan.create_from_signal(
            session, user_id, "300001", "volume_surge", signal_date=signal
        )
        assert created_again is False
        assert again["id"] == plan["id"]
        assert session.get(TradePlan, plan["id"]).buy_price == 99.0


def test_from_signal_overwrite_true_uses_existing_row(tmp_path):
    engine = _engine(tmp_path, "p2d-overwrite.db")
    user_id, signal, target = _seed_signal(engine)
    with Session(engine) as session:
        plan, _ = trade_plan.create_from_signal(
            session, user_id, "300001", "volume_surge", signal_date=signal
        )
        row = session.get(TradePlan, plan["id"])
        row.buy_price = 99.0
        session.commit()

        again, created = trade_plan.create_from_signal(
            session,
            user_id,
            "300001",
            "volume_surge",
            signal_date=signal,
            overwrite=True,
        )
        assert created is False
        assert again["id"] == plan["id"]
        assert again["buy_price"] == 10.8
        assert (
            session.scalar(
                select(func.count())
                .select_from(TradePlan)
                .where(
                    TradePlan.user_id == user_id,
                    TradePlan.code == "300001",
                    TradePlan.plan_date == target,
                )
            )
            == 1
        )


def test_from_signal_limit_full_different_key_rejected(tmp_path):
    """200 条已满、目标又是一条不同的新计划：仍要明确拒绝。"""
    engine = _engine(tmp_path, "p2d-full.db")
    user_id, signal, _ = _seed_signal(engine, plans=trade_plan.MAX_PLANS)
    with Session(engine) as session:
        with pytest.raises(ValueError, match="交易计划最多"):
            trade_plan.create_from_signal(
                session, user_id, "300001", "volume_surge", signal_date=signal
            )
