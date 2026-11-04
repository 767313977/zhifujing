"""Devin 全面审查的整改（2026-11-04，基线 667d8c5）。

钉四件事：
1. `/api/patterns/preview` 同时传形态与阶段不再 500，且徽标数 = `/hits` 行数；
2. 交易日志 / 持仓 / 计划执行三处对同一批非法入参都给 400（共用 `validate_entry`）；
3. 杀猪盘体检换 context 不再重新打外部源（缓存只按 code，背景现叠加）；
4. 未登录时登出返回 200（`/api/auth/logout` 在免登录名单里）。
"""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.api import auth as auth_api
from app.api import journal as journal_api
from app.api import patterns as patterns_api
from app.api import plans as plans_api
from app.api import positions as positions_api
from app.api import stock as stock_api
from app.api import watchlist as watchlist_api
from app.models import (
    AppUser,
    Base,
    PatternHit,
    StockBasic,
    StockDaily,
    TradeCalendar,
    TradePlan,
    Watchlist,
)
from app.schemas import (
    InviteIn,
    PlanExecuteIn,
    PositionIn,
    TradeJournalIn,
    WatchlistPatchIn,
)
from app.services import auth, trap
from app.services.auth import PUBLIC_PATHS  # noqa: F401  - 见 logout 用例
from app.timeutil import today_cn

DAY = date(2026, 10, 30)
DAYS = [DAY - timedelta(days=offset) for offset in range(20)][::-1]
#: 日历里没有这一天（DAY 的下一天）：用来触发「不是交易日」
NON_TRADE = DAY + timedelta(days=1)
FUTURE = today_cn() + timedelta(days=3)
PATTERN = "volume_surge"


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'devin.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([TradeCalendar(trade_date=day) for day in DAYS])
        # 日历还要铺到 DAY **之后**：否则 DAY+1 落在覆盖范围之外，校验会按「判断不了」
        # 放行（跨年场景，见 `test_trade_journal.py` 那三条），测不到「不是交易日」
        session.add_all(
            [
                TradeCalendar(trade_date=DAY + timedelta(days=offset))
                for offset in (2, 3, 4)
            ]
        )
        session.add(StockBasic(code="300001", name="测试A"))
        for day in DAYS:
            session.add(
                StockDaily(trade_date=day, code="300001", close=10.0, pct_chg=1.0, amount=1e8)
            )
        session.add(
            PatternHit(
                trade_date=DAY,
                code="300001",
                pattern=PATTERN,
                name="测试A",
                score=60.0,
                close=10.0,
                key_levels={},
                detail={},
            )
        )
        user = AppUser(username="u1", password_hash="x")
        session.add(user)
        session.flush()
        session.add(
            TradePlan(
                user_id=user.id,
                code="300001",
                name="测试A",
                plan_date=DAY,
                buy_price=10.0,
                stop_price=9.0,
            )
        )
        session.commit()
        yield session, user


# ---------------------------------------------------------------- 1. preview


def test_preview_with_pattern_and_phase_matches_hits(db):
    """形态 + 阶段同时选：以前 `list &= set` 直接 TypeError → 必 500。"""
    session, _ = db
    badge = {
        row.pattern: row.stocks
        for row in patterns_api.preview(
            db=session,
            trade_date=DAY,
            min_score=0.0,
            pattern=[PATTERN],
            days=1,
            phase=["oversold"],
        )
    }
    hits = patterns_api.hits(
        db=session,
        trade_date=DAY,
        min_score=0.0,
        pattern=[PATTERN],
        days=1,
        limit=800,
        order="score",
        phase=["oversold"],
    )
    assert badge[PATTERN] == len(hits.items)
    # 再钉一次「徽标 = 行数」的方向：只有 1 只票命中，且它是否超跌由同一份
    # `_market_oversold` 决定 —— 两边必须一起变
    assert badge[PATTERN] in (0, 1)


# ------------------------------------------------- 2. 三处建仓校验同一份


CASES = [
    pytest.param({"entry_date": NON_TRADE}, id="非交易日"),
    pytest.param({"entry_price": 0.0}, id="买入价为0"),
    pytest.param({"shares": 0}, id="股数为0"),
    pytest.param({"position_pct": 150.0}, id="仓位越界"),
    pytest.param({"entry_date": FUTURE}, id="买入日在未来"),
]


def _base(**over):
    data = {"code": "300001", "entry_date": DAY, "entry_price": 10.0, "shares": 100}
    data.update(over)
    return data


@pytest.mark.parametrize("over", CASES)
def test_positions_reject_invalid_entry(db, over):
    session, user = db
    with pytest.raises(Exception) as info:  # noqa: PT011 - 统一断言 400
        positions_api.create_position(
            payload=PositionIn(**_base(**over)), session=session, user=user
        )
    assert info.value.status_code == 400


@pytest.mark.parametrize("over", CASES)
def test_journal_reject_invalid_entry(db, over):
    session, user = db
    with pytest.raises(Exception) as info:  # noqa: PT011
        journal_api.create_trade(
            payload=TradeJournalIn(**_base(**over)), session=session, user=user
        )
    assert info.value.status_code == 400


@pytest.mark.parametrize("over", CASES)
def test_plan_execute_rejects_invalid_entry(db, over):
    session, user = db
    plan = session.query(TradePlan).first()
    # 计划回填的字段叫 `actual_price`，而三处通用的用例里写的是 `entry_price`
    rest = {key: val for key, val in over.items() if key != "entry_price"}
    data = {"actual_price": over.get("entry_price", 10.0), "shares": 100, **rest}
    data.pop("code", None)
    with pytest.raises(Exception) as info:  # noqa: PT011
        plans_api.execute_plan(
            plan_id=plan.id, payload=PlanExecuteIn(**data), session=session, user=user
        )
    assert info.value.status_code == 400


# ------------------------------------------------- 3. 体检：换背景不重打外网


def test_trap_context_does_not_bypass_cache(db, monkeypatch):
    session, user = db
    calls: list[str] = []

    def fake_base(code: str, *, fast: bool = False):
        calls.append(code)
        return trap.TrapResult(
            code=code,
            name="测试A",
            trade_date=DAY,
            score=0,
            level="low",
            level_label="🟢 安全",
            summary="已核对的信号均未命中",
        )

    monkeypatch.setattr(stock_api.trap, "base_check", fake_base)
    stock_api._trap_cache.clear()

    strong = stock_api.trap_check(
        code="300001", context="老师带单", session=session, user=user
    )
    weak = stock_api.trap_check(
        code="300001", context="群里说的", session=session, user=user
    )

    assert len(calls) == 1  # 外部取数只打了一遍
    assert strong.context_boost >= weak.context_boost  # 背景照旧生效
    assert weak.summary.endswith("档")  # 摘要里带上了背景说明
    # 缓存里那份基准结果没有被背景污染（signals 没被就地追加）
    assert stock_api._trap_cache["300001"][1].signals == []


# ------------------------------------------------------------- 4. 低风险项


def test_logout_without_session_returns_ok(db):
    session, _ = db
    assert "/api/auth/logout" in auth.PUBLIC_PATHS
    result = auth_api.logout(
        request=SimpleNamespace(cookies={}), response=Response(), db=session
    )
    assert result == {"ok": True}


def test_invite_count_above_20_is_rejected():
    with pytest.raises(ValidationError):
        InviteIn(count=21)
    assert InviteIn(count=20).count == 20


def test_watchlist_patch_does_not_need_code(db):
    session, user = db
    session.add(Watchlist(user_id=user.id, code="300001", name="测试A"))
    session.commit()
    row = watchlist_api.update_watchlist(
        code="300001", payload=WatchlistPatchIn(note="新备注"), user=user, session=session
    )
    assert row.note == "新备注"
