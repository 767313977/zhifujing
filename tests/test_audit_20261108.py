"""2026-11-08 审计整改的回归测试（P1-01/P1-02/P2-01~P2-07）。

钉六件事：
1. 「从计划转持仓」幂等：连点两次只有一笔日志、返回同一个 id；
2. 数据库层兜底：同一 (user, plan) 插第二笔会被唯一索引拒绝；
3. 计划回填与转持仓在同一个事务里落库（计划状态 + 日志）；
4. 行情早于买入日时**不给浮盈**（不拿昨天的收盘当现价）；
5. 登录对「用户名不存在」也真的跑一次密码校验（防时间侧信道）；
6. 日志 500 / 计划 200 的上限在别的写入口同样生效。
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response

from app.api import auth as auth_api
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
)
from app.schemas import LoginIn
from app.services import auth, positions, trade_journal, trade_plan

DAY = date(2026, 11, 6)
PREV = DAY - timedelta(days=1)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([TradeCalendar(trade_date=PREV), TradeCalendar(trade_date=DAY)])
        session.add(StockBasic(code="300001", name="测试A"))
        user = AppUser(username="u1", password_hash="x")
        session.add(user)
        session.flush()
        plan = TradePlan(
            user_id=user.id,
            code="300001",
            name="测试A",
            plan_date=DAY,
            buy_price=10.0,
            stop_price=9.0,
            source="volume_surge",
        )
        session.add(plan)
        session.commit()
        yield session, user, plan


# ------------------------------------------------- 1/2. 转持仓幂等 + 唯一索引


def test_from_plan_is_idempotent(db):
    session, user, plan = db
    first = positions_api.create_from_plan(plan_id=plan.id, session=session, user=user)
    second = positions_api.create_from_plan(plan_id=plan.id, session=session, user=user)

    assert first.id == second.id
    journals = list(
        session.scalars(select(TradeJournal).where(TradeJournal.plan_id == plan.id))
    )
    assert len(journals) == 1
    session.refresh(plan)
    assert plan.status == "executed"


def test_unique_index_blocks_second_journal_for_same_plan(db):
    session, user, plan = db
    session.add(
        TradeJournal(
            user_id=user.id,
            code="300001",
            entry_date=DAY,
            entry_price=10.0,
            plan_id=plan.id,
        )
    )
    session.commit()

    session.add(
        TradeJournal(
            user_id=user.id,
            code="300001",
            entry_date=DAY,
            entry_price=10.5,
            plan_id=plan.id,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# ------------------------------------------------- 3. 行情早于买入日不给浮盈


def test_position_hides_pnl_when_quote_is_before_entry(db):
    session, user, plan = db
    # 只有 T-1 的收盘 11 元；T 日（买入日）还没有日线
    session.add(StockDaily(trade_date=PREV, code="300001", close=11.0, pct_chg=10.0))
    session.commit()
    journal = positions.create(
        session, user.id, code="300001", entry_date=DAY, entry_price=10.0, shares=1000
    )

    row = {item["id"]: item for item in positions.load(session, user.id)}[journal.id]
    assert row["latest_date"] == PREV
    assert row["latest_close"] is None
    assert row["pnl"] is None
    assert row["pnl_pct"] is None
    assert row["market_value"] is None


# ------------------------------------------------- 4. 登录时间侧信道


def _request(ip: str = "10.9.9.9") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "headers": [(b"host", b"127.0.0.1")],
            "client": (ip, 1234),
            "scheme": "http",
            "query_string": b"",
        }
    )


def test_login_verifies_password_even_for_unknown_user(db, monkeypatch):
    session, _, _ = db
    calls: list[str] = []

    def _spy(password: str, stored: str) -> bool:
        calls.append(password)
        return False

    monkeypatch.setattr(auth, "verify_password", _spy)
    monkeypatch.setattr(auth, "guard_rate", lambda ip: None)
    monkeypatch.setattr(auth, "guard_user_rate", lambda username, ip: None)
    monkeypatch.setattr(auth.login_limiter, "fail", lambda key: None)
    monkeypatch.setattr(auth.login_user_limiter, "fail", lambda key: None)

    with pytest.raises(Exception) as info:
        auth_api.login(
            payload=LoginIn(username="nobody", password="secret123"),
            request=_request(),
            response=Response(),
            db=session,
        )
    assert info.value.status_code == 401
    # 用户名不存在也必须真的跑一次校验（不能因为 or 短路而跳过 scrypt）
    assert calls == ["secret123"]


def test_session_cookie_lives_to_absolute_limit(db):
    response = Response()
    auth_api._set_cookie(response, "token", _request())
    cookie = response.headers["set-cookie"]
    assert f"Max-Age={auth.SESSION_ABSOLUTE_DAYS * 24 * 3600}" in cookie


# ------------------------------------------------- 5. 上限收敛到服务层


def test_journal_limit_applies_to_positions(db, monkeypatch):
    session, user, _ = db
    monkeypatch.setattr(trade_journal, "MAX_ENTRIES", 0)
    with pytest.raises(ValueError, match="交易日志最多"):
        positions.create(
            session, user.id, code="300001", entry_date=DAY, entry_price=10.0
        )


def test_plan_limit_applies_to_from_signal(db, monkeypatch):
    session, user, _ = db
    session.add(
        PatternHit(
            trade_date=DAY,
            code="300001",
            pattern="volume_surge",
            name="测试A",
            score=80.0,
            close=10.5,
            key_levels={"breakout": 10.8, "support": 9.9},
            detail={},
        )
    )
    # 次日要有日历，`next_trade_day` 才取得到目标日
    session.add(TradeCalendar(trade_date=DAY + timedelta(days=1)))
    session.commit()
    monkeypatch.setattr(trade_plan, "MAX_PLANS", 0)

    with pytest.raises(ValueError, match="交易计划最多"):
        trade_plan.create_from_signal(
            session, user.id, "300001", "volume_surge", signal_date=DAY
        )


def test_plan_limit_does_not_block_existing_plan(db, monkeypatch):
    """已达上限时，重复生成**同一只票同一天**的计划不该被挡（那是更新）。"""
    session, user, _ = db
    session.add(
        PatternHit(
            trade_date=DAY,
            code="300001",
            pattern="volume_surge",
            name="测试A",
            score=80.0,
            close=10.5,
            key_levels={"breakout": 10.8, "support": 9.9},
            detail={},
        )
    )
    session.add(TradeCalendar(trade_date=DAY + timedelta(days=1)))
    session.commit()

    monkeypatch.setattr(trade_plan, "MAX_PLANS", 10)
    first, created = trade_plan.create_from_signal(
        session, user.id, "300001", "volume_surge", signal_date=DAY
    )
    assert created is True

    # 库里现在 2 条（夹具 1 + 刚生成 1），上限正好卡在 2：再新增会拒，但重复生成是更新
    monkeypatch.setattr(trade_plan, "MAX_PLANS", 2)
    again, created_again = trade_plan.create_from_signal(
        session, user.id, "300001", "volume_surge", signal_date=DAY
    )
    assert created_again is False
    assert again["id"] == first["id"]
