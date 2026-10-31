"""2026-10-23 审计修复的回归测试（task-7）。

覆盖的**新增行为**：
- `resolve_trade_date` 显式传非交易日 → 404（默认不传的行为不变）
- 次日计划拒绝 `stock_basic` 里不存在的代码 → 400
- 个股 profile 在「无 stock_basic 且无日线」时 → 404（有日线仍 200）
- SPA 兜底遇 null byte / 扫描路径 → 404（不再 500）
- `/api/health` 同时注册 GET 与 HEAD
- 过期会话清理口径（滑动过期 + 90 天绝对上限）
- 杀猪盘体检 TTL 缓存（成功缓存 / 失败不缓存）
- `_expected_latest` 用北京时间（不再用进程墙钟）
- 三处新增索引已声明
- 两个新配置项可用大写环境变量覆盖
- 形态缓存预热跑满三步、单步失败不影响其余
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import models
from app.api import deps
from app.api import patterns as patterns_api
from app.api import plans as plans_api
from app.api import stock as stock_api
from app.config import Settings
from app.main import _is_scan_probe, _spa_response, _warm_pattern_cache, app
from app.models import (
    AppUser,
    MarketSentiment,
    StockBasic,
    StockDaily,
    TradeCalendar,
    UserSession,
)
from app.schemas import TradePlanIn
from app.services import auth, trap
from app.timeutil import now_cn

DAY = date(2026, 10, 23)
PREV = date(2026, 10, 22)


@pytest.fixture()
def db(tmp_path):
    """独立临时 SQLite（不碰 backend/data/fupan.db）。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'audit-fixes.db'}")
    models.Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with session_factory() as session:
        yield session
    engine.dispose()


def _user(user_id: int = 1) -> AppUser:
    return AppUser(id=user_id, username=f"user{user_id}", password_hash="x")


def _plan(code: str = "999999", plan_date: date = DAY) -> TradePlanIn:
    return TradePlanIn(
        code=code, name=None, plan_date=plan_date, buy_price=10.0, stop_price=9.0
    )


# ---------------------------------------------------------------- resolve_trade_date


def test_resolve_trade_date_rejects_non_trading_day(db):
    with pytest.raises(HTTPException) as exc:
        deps.resolve_trade_date(trade_date=date(1900, 1, 1), session=db)
    assert exc.value.status_code == 404
    assert "不是交易日" in exc.value.detail


def test_resolve_trade_date_accepts_trading_day(db):
    db.add(TradeCalendar(trade_date=DAY))
    db.commit()
    assert deps.resolve_trade_date(trade_date=DAY, session=db) == DAY


def test_resolve_trade_date_default_uses_latest_sentiment(db):
    db.add_all(
        [
            MarketSentiment(trade_date=PREV, limit_up_count=10),
            MarketSentiment(trade_date=DAY, limit_up_count=12),
        ]
    )
    db.commit()
    assert deps.resolve_trade_date(trade_date=None, session=db) == DAY


def test_resolve_trade_date_default_empty_db_still_404(db):
    with pytest.raises(HTTPException) as exc:
        deps.resolve_trade_date(trade_date=None, session=db)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------- 次日计划：未知代码


def test_plans_rejects_unknown_code(db):
    db.add(TradeCalendar(trade_date=DAY))
    db.commit()
    with pytest.raises(HTTPException) as exc:
        plans_api._validate(db, _plan())
    assert exc.value.status_code == 400
    assert "查不到这个代码" in exc.value.detail


def test_plans_accepts_known_code_and_fills_name(db):
    db.add_all(
        [TradeCalendar(trade_date=DAY), StockBasic(code="000001", name="平安银行")]
    )
    db.commit()
    assert plans_api._validate(db, _plan(code="000001")) == ("000001", "平安银行")


# ---------------------------------------------------------------- 个股 profile：未知代码


def test_profile_unknown_code_404(db):
    with pytest.raises(HTTPException) as exc:
        stock_api.profile("999999", user=_user(), session=db)
    assert exc.value.status_code == 404


def test_profile_with_daily_but_no_basic_still_200(db, monkeypatch):
    # ⚠️ `profile` 会算阶段判定，而 `stock_phase._trade_day_index` 查的是**全局
    # session_scope**（生产库）的交易日历，不是本测试的临时 session —— 本地库里有表
    # 就通过、CI 的空库上就会 `no such table: trade_calendar`（2026-10-23 CI 实际踩到）。
    # 这里把它钉成空表即可：本用例要验的是「有日线、无 stock_basic 仍 200」，
    # 不是阶段判定的算法。生产环境里两者是同一个库，不受影响。
    from app.services import stock_phase

    monkeypatch.setattr(stock_phase, "_trade_day_index", lambda refresh=False: {})
    db.add(
        StockDaily(
            trade_date=DAY,
            code="000001",
            name="平安银行",
            open=10.0,
            high=10.2,
            low=9.9,
            close=10.1,
            pct_chg=1.0,
            volume=1e6,
            amount=1e7,
            turnover=1.0,
        )
    )
    db.commit()
    result = stock_api.profile("000001", user=_user(), session=db)
    assert result.code == "000001"
    assert result.day_count == 1


# ---------------------------------------------------------------- 非法输入：控制字符

def test_resolve_code_rejects_control_chars():
    """`600519\\x00` 以前会被抽成 600519 正常返回（非法输入被静默接受）。"""
    from app.services.stock_lookup import LookupError as StockLookupError
    from app.services.stock_lookup import resolve_code

    for bad in ("600519\x00", "贵州\x00茅台", "\x1b[31m600519"):
        with pytest.raises(StockLookupError):
            resolve_code(bad)


# ---------------------------------------------------------------- SPA 兜底 / 扫描路径


@pytest.mark.parametrize("path", ["\x00", "foo\x00bar"])
def test_spa_response_null_byte_returns_404(path):
    with pytest.raises(HTTPException) as exc:
        _spa_response(path)
    assert exc.value.status_code == 404


def test_spa_response_api_prefix_404():
    with pytest.raises(HTTPException) as exc:
        _spa_response("api/admin/status")
    assert exc.value.status_code == 404


@pytest.mark.parametrize(
    "path",
    ["docs", "openapi.json", "redoc", "fupan.db", "fupan.db-wal", "x/data.sqlite"],
)
def test_scan_probes_rejected(path):
    assert _is_scan_probe(path)


def test_normal_frontend_route_not_treated_as_scan_probe():
    assert not _is_scan_probe("watchlist")
    assert not _is_scan_probe("stock/000001")


# ---------------------------------------------------------------- HEAD 支持


def test_health_route_supports_get_and_head():
    """HEAD 仍然可用（`curl -I` 判活），2026-10-31 起与 GET **分成两条路由**。

    原来两个 method 挂在同一条 `api_route` 上，OpenAPI 会生成两个同名 operationId；
    拆开之后 HEAD 走 `include_in_schema=False` 的独立路由。
    """
    routes = [route for route in app.routes if getattr(route, "path", None) == "/api/health"]
    assert routes, "没找到 /api/health 路由"
    methods = set().union(*(set(route.methods) for route in routes))
    assert {"GET", "HEAD"} <= methods


# ---------------------------------------------------------------- 过期会话清理


def test_delete_expired_sessions(db):
    now = datetime.now()
    db.add(AppUser(id=1, username="u", password_hash="x"))
    db.add_all(
        [
            # 滑动过期
            UserSession(
                token_hash="expired",
                user_id=1,
                created_at=now - timedelta(days=31),
                expires_at=now - timedelta(days=1),
            ),
            # 有效
            UserSession(
                token_hash="valid",
                user_id=1,
                created_at=now - timedelta(days=10),
                expires_at=now + timedelta(days=20),
            ),
            # expires_at 还没到，但 created_at 已超 90 天绝对上限
            UserSession(
                token_hash="absolute",
                user_id=1,
                created_at=now - timedelta(days=auth.SESSION_ABSOLUTE_DAYS + 1),
                expires_at=now + timedelta(days=10),
            ),
        ]
    )
    db.commit()
    assert auth.delete_expired_sessions(db) == 2
    remaining = {row.token_hash for row in db.scalars(select(UserSession))}
    assert remaining == {"valid"}


# ---------------------------------------------------------------- 体检缓存


def _trap_result() -> trap.TrapResult:
    return trap.TrapResult(
        code="000001",
        name="平安银行",
        trade_date=DAY,
        score=10,
        level="low",
        level_label="🟢 安全",
        summary="测试用",
        signals=[],
    )


def test_trap_result_is_cached(db, monkeypatch):
    calls: list[tuple[str, str | None]] = []

    def fake_check(code, context):
        calls.append((code, context))
        return _trap_result()

    monkeypatch.setattr(stock_api.trap, "check", fake_check)
    stock_api._trap_cache.clear()
    first = stock_api.trap_check("000001", None, db, _user())
    second = stock_api.trap_check("000001", None, db, _user())
    assert len(calls) == 1
    assert first.score == second.score == 10
    stock_api._trap_cache.clear()


def test_trap_failure_is_not_cached(db, monkeypatch):
    calls: list[str] = []

    def fake_check(code, context):
        calls.append(code)
        return None

    monkeypatch.setattr(stock_api.trap, "check", fake_check)
    stock_api._trap_cache.clear()
    for _ in range(2):
        with pytest.raises(HTTPException) as exc:
            stock_api.trap_check("999999", None, db, _user())
        assert exc.value.status_code == 400
    assert len(calls) == 2
    stock_api._trap_cache.clear()


# ---------------------------------------------------------------- 时区：expected_latest


def test_expected_latest_uses_beijing_clock(db, monkeypatch):
    db.add_all([TradeCalendar(trade_date=PREV), TradeCalendar(trade_date=DAY)])
    db.commit()
    tz = now_cn().tzinfo
    monkeypatch.setattr(stock_api, "today_cn", lambda: DAY)
    # 北京时间 16:00（已过 15:05 收盘）→ 今天就是"应该有终值"的最近交易日
    monkeypatch.setattr(
        stock_api, "now_cn", lambda: datetime(2026, 10, 23, 16, 0, tzinfo=tz)
    )
    assert stock_api._expected_latest(db) == DAY
    # 北京时间 10:00（未收盘）→ 只能算到昨天
    monkeypatch.setattr(
        stock_api, "now_cn", lambda: datetime(2026, 10, 23, 10, 0, tzinfo=tz)
    )
    assert stock_api._expected_latest(db) == PREV


# ---------------------------------------------------------------- 新增索引


def test_new_indexes_declared():
    assert "ix_trade_t_user_date" in {index.name for index in models.TradeT.__table__.indexes}
    assert "ix_limit_pool_pooltype_date" in {
        index.name for index in models.LimitPool.__table__.indexes
    }
    assert "ix_index_daily_code_date" in {
        index.name for index in models.IndexDaily.__table__.indexes
    }


# ---------------------------------------------------------------- 配置项


def test_new_settings_can_be_overridden_by_env(monkeypatch):
    monkeypatch.setenv("ENABLE_DEV_CORS", "false")
    monkeypatch.setenv("WARMUP_PATTERN_CACHE", "false")
    configured = Settings()
    assert configured.enable_dev_cors is False
    assert configured.warmup_pattern_cache is False
    # 默认值（.env 不设这两项时）
    monkeypatch.delenv("ENABLE_DEV_CORS")
    monkeypatch.delenv("WARMUP_PATTERN_CACHE")
    defaults = Settings()
    assert defaults.enable_dev_cors is True
    assert defaults.warmup_pattern_cache is True


# ---------------------------------------------------------------- 形态缓存预热


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_warm_pattern_cache_runs_all_steps(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(patterns_api, "scorecard", lambda **kw: calls.append("scorecard"))
    monkeypatch.setattr(patterns_api, "standing", lambda **kw: calls.append("standing"))
    monkeypatch.setattr(patterns_api, "track", lambda **kw: calls.append("track"))
    monkeypatch.setattr("app.db.SessionLocal", lambda: _FakeSession())

    _warm_pattern_cache()
    assert calls == ["scorecard", "standing", "track"]


def test_warm_pattern_cache_failure_does_not_stop_others(monkeypatch):
    calls: list[str] = []

    def boom(**kw):
        raise RuntimeError("模拟预热失败")

    monkeypatch.setattr(patterns_api, "scorecard", boom)
    monkeypatch.setattr(patterns_api, "standing", lambda **kw: calls.append("standing"))
    monkeypatch.setattr(patterns_api, "track", lambda **kw: calls.append("track"))
    monkeypatch.setattr("app.db.SessionLocal", lambda: _FakeSession())

    _warm_pattern_cache()  # 不应抛出
    assert calls == ["standing", "track"]


# ---------------------------------------------------------------- 过期会话清理 job


def test_session_cleanup_job_registered_daily_at_0330():
    from app.jobs import scheduler as scheduler_mod

    configured = Settings(
        scheduler_enabled=True,
        catchup_on_start=False,
        backup_enabled=False,
        alert_enabled=False,
        b1_intraday_enabled=False,
        session_cleanup_enabled=True,
    )
    daily = scheduler_mod.DailyScheduler(configured)
    daily.start()
    try:
        assert daily._scheduler is not None
        job = daily._scheduler.get_job(scheduler_mod.SESSION_CLEANUP_JOB_ID)
        assert job is not None
        assert job.max_instances == 1
        fields = {field.name: str(field) for field in job.trigger.fields}
        assert fields["hour"] == "3"
        assert fields["minute"] == "30"
    finally:
        daily.shutdown()
