"""P3 收尾（2026-11-01）：连接池、持仓归属、批量 IN、共享 iFinD 客户端、
tencent / em_news / eastmoney 的取数健壮性、交易日自愈、配额档位单一来源、
简报 fast 路径只读。

这些多是「参数 / 单一来源」类的改动，回归测试只锁**行为不变的部分**：
判档仍按原阈值、批量查询结果与单条 SQL 一致、异常路径不再拖垮整帧。
"""

from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import db as db_module
from app.models import AppUser, Base, StockBasic, StockDaily, StockFinancial, TradeJournal, TradePlan
from app.services import positions as positions_service


def _session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


# ---------------------------------------------------------------- #4 连接池


def test_engine_pool_matches_request_executor():
    """池要跟 40 个请求线程匹配，排队上限压到 10 秒。"""
    assert db_module.engine.pool.size() == 20
    assert db_module.engine.pool._max_overflow == 20
    assert db_module.engine.pool._timeout == 10


# ---------------------------------------------------------------- #2 持仓归属


def test_create_position_rejects_other_users_plan():
    session = _session()
    session.add_all(
        [
            AppUser(id=1, username="a", password_hash="x"),
            AppUser(id=2, username="b", password_hash="x"),
            StockBasic(code="600519", name="贵州茅台"),
        ]
    )
    session.add(
        TradePlan(
            user_id=2,
            code="600519",
            plan_date=date(2026, 10, 30),
            buy_price=10.0,
            stop_price=9.0,
        )
    )
    session.commit()
    with pytest.raises(ValueError):
        positions_service.create(
            session,
            1,
            code="600519",
            entry_date=date(2026, 10, 30),
            entry_price=10.0,
            plan_id=1,
        )
    # 不存在的计划同样拒绝
    with pytest.raises(ValueError):
        positions_service.create(
            session,
            1,
            code="600519",
            entry_date=date(2026, 10, 30),
            entry_price=10.0,
            plan_id=999,
        )
    # 自己的计划可以通过，且 plan_id 落库
    journal = positions_service.create(
        session,
        2,
        code="600519",
        entry_date=date(2026, 10, 30),
        entry_price=10.0,
        plan_id=1,
    )
    assert journal.plan_id == 1
    assert session.query(TradeJournal).count() == 1


# ---------------------------------------------------------------- #7 批量 IN


def test_code_batches_splits_without_losing_codes():
    from app.jobs.collect_kline import _IN_BATCH, _code_batches

    codes = [f"{index:06d}" for index in range(_IN_BATCH * 2 + 7)]
    batches = _code_batches(codes)
    assert [len(batch) for batch in batches] == [_IN_BATCH, _IN_BATCH, 7]
    assert [code for batch in batches for code in batch] == codes
    assert _code_batches([]) == []


def test_count_pending_sums_across_batches(monkeypatch):
    """1000+ 只（跨多个 IN 批）时判据与单批 SQL 同结果：全采到 → 0 天待采。"""
    from app.jobs import collect_kline

    session = _session()
    day = date(2026, 10, 30)
    codes = [f"{index:06d}" for index in range(1000)]
    session.add_all(
        [
            StockDaily(
                trade_date=day,
                code=code,
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                pct_chg=0.0,
                volume=1.0,
                amount=1.0,
            )
            for code in codes
        ]
    )
    session.commit()

    from contextlib import contextmanager

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(collect_kline, "session_scope", _scope)
    monkeypatch.setattr(collect_kline, "_short_days", lambda: set())

    collector = collect_kline.KlineCollector.__new__(collect_kline.KlineCollector)
    assert collector._count_pending([day], codes, tail=1) == 0
    # 少一半票时判「缺」—— 证明跨批求和没有丢批
    assert collector._count_pending([day], codes + [f"{index:06d}" for index in range(1000, 2000)], tail=1) == 1


# ---------------------------------------------------------------- #6 线程泄漏计数


def test_call_bounded_counts_abandoned_threads():
    import time

    from app.sources.akshare_source import _call_bounded, abandoned_calls

    before = abandoned_calls()
    with pytest.raises(TimeoutError):
        _call_bounded(lambda: (time.sleep(0.3), pd.DataFrame())[1], "测试", 0.05)
    assert abandoned_calls() == before + 1


# ---------------------------------------------------------------- #9 em_news 限时


def test_fetch_stock_news_goes_through_bounded_call(monkeypatch):
    from app.sources import em_news

    called: list[str] = []

    def _bounded(fn, description, timeout):
        called.append(description)
        return pd.DataFrame(
            [{"新闻标题": "600519 公告", "新闻内容": "正文", "发布时间": "2026-10-30", "文章来源": "x", "新闻链接": ""}]
        )

    monkeypatch.setattr(em_news, "_call_bounded", _bounded)
    result = em_news.fetch_stock_news("600519", name="贵州茅台", limit=5)
    assert called and "600519" in called[0]
    assert len(result.items) == 1


# ---------------------------------------------------------------- #10 tencent 逐批降级


def test_fetch_spot_keeps_good_batches_when_one_fails(monkeypatch):
    from app.sources import tencent

    # 腾讯快照是「~」分隔的位置数组，字段不够会被判成异常行丢掉
    fields = ["1", "贵州茅台", "600519"] + ["1"] * 60
    payload = "v_sh600519=" + '"' + "~".join(fields) + '"'

    class _Resp:
        content = (payload + ";").encode("gbk")

        def raise_for_status(self):
            return None

    calls: list[str] = []

    def _get(url, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return _Resp()

    monkeypatch.setattr(tencent, "_SPOT_BATCH", 1)
    monkeypatch.setattr(tencent.requests, "get", _get)
    rows = tencent.fetch_spot(["600519", "000001"])
    assert len(calls) == 2
    assert rows and rows[0]["code"] == "600519"


def test_fetch_spot_raises_when_every_batch_fails(monkeypatch):
    from app.sources import tencent

    def _get(url, timeout=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(tencent, "_SPOT_BATCH", 1)
    monkeypatch.setattr(tencent.requests, "get", _get)
    with pytest.raises(RuntimeError):
        tencent.fetch_spot(["600519"])


# ---------------------------------------------------------------- #12 eastmoney 不改全局 env


def test_fetch_daily_does_not_touch_process_proxy_env(monkeypatch):
    from app.sources import eastmoney

    monkeypatch.setenv("ENABLE_PUSH2HIS", "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setattr(eastmoney, "_RETRIES", 1)

    class _Session:
        trust_env = True

        def get(self, *args, **kwargs):
            raise RuntimeError("down")

    monkeypatch.setattr("requests.Session", lambda: _Session())
    with pytest.raises(RuntimeError):
        eastmoney.fetch_daily("600519", days=10)
    assert __import__("os").environ.get("HTTPS_PROXY") == "http://127.0.0.1:7890"


# ---------------------------------------------------------------- #13 交易日自愈


def test_is_trade_day_fresh_skips_refresh_when_hit(monkeypatch):
    from app.services import trading_days

    monkeypatch.setattr(trading_days, "is_trade_day", lambda day, session=None: True)

    def _boom(*args, **kwargs):
        raise AssertionError("命中时不该补日历")

    monkeypatch.setattr(trading_days, "calendar_needs_refresh", _boom)
    assert trading_days.is_trade_day_fresh(date(2026, 10, 30)) is True


def test_is_trade_day_fresh_refreshes_stale_calendar(monkeypatch):
    from app.services import trading_days

    results = iter([False, True])
    monkeypatch.setattr(trading_days, "is_trade_day", lambda day, session=None: next(results))
    monkeypatch.setattr(trading_days, "calendar_needs_refresh", lambda session=None: True)

    refreshed: list[bool] = []

    class _Collector:
        def collect_calendar(self):
            refreshed.append(True)

    import app.jobs.collect_daily as collect_daily

    monkeypatch.setattr(collect_daily, "DailyCollector", _Collector)
    assert trading_days.is_trade_day_fresh(date(2026, 1, 5)) is True
    assert refreshed == [True]


def test_is_trade_day_fresh_stays_false_when_calendar_is_current(monkeypatch):
    from app.services import trading_days

    monkeypatch.setattr(trading_days, "is_trade_day", lambda day, session=None: False)
    monkeypatch.setattr(trading_days, "calendar_needs_refresh", lambda session=None: False)

    import app.jobs.collect_daily as collect_daily

    class _Collector:
        def __init__(self):
            raise AssertionError("日历没过期不该构造采集器")

    monkeypatch.setattr(collect_daily, "DailyCollector", _Collector)
    assert trading_days.is_trade_day_fresh(date(2026, 10, 31)) is False


# ---------------------------------------------------------------- #15 档位单一来源


def test_level_for_follows_level_budgets_table(monkeypatch):
    from app.services import usage

    monkeypatch.setattr(
        usage,
        "LEVEL_BUDGETS",
        ((300, usage.QuotaLevel.NORMAL), (100, usage.QuotaLevel.PAUSE_KLINE), (0, usage.QuotaLevel.CORE_ONLY)),
    )
    assert usage._level_for(300, 1) == usage.QuotaLevel.NORMAL
    assert usage._level_for(299, 1) == usage.QuotaLevel.PAUSE_KLINE
    assert usage._level_for(99, 1) == usage.QuotaLevel.CORE_ONLY


def test_level_for_keeps_default_thresholds():
    from app.services import usage

    assert usage._level_for(1000, 5) == usage.QuotaLevel.NORMAL
    assert usage._level_for(700, 5) == usage.QuotaLevel.PAUSE_HIT_DDE
    assert usage._level_for(70, 1) == usage.QuotaLevel.PAUSE_KLINE
    assert usage._level_for(0, 0) == usage.QuotaLevel.CORE_ONLY


# ---------------------------------------------------------------- #16 财务摘要只读


def test_financials_latest_allow_fetch_false_never_requests(monkeypatch):
    from contextlib import contextmanager

    from app.services import financials

    session = _session()
    session.add(
        StockFinancial(
            code="600519",
            report_period=date(2025, 12, 31),
            revenue=1.0,
            fetched_at=datetime.now() - timedelta(days=financials.CACHE_DAYS + 30),
        )
    )
    session.commit()

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(financials, "session_scope", _scope)
    monkeypatch.setattr(
        financials.AkshareSource,
        "financial_abstract_ths",
        lambda self, code: (_ for _ in ()).throw(AssertionError("不该联网")),
    )
    snapshot = financials.latest("600519", allow_fetch=False)
    assert snapshot is not None and snapshot.report_period == date(2025, 12, 31)
    # 没有缓存时也只返回 None，不联网
    assert financials.latest("000001", allow_fetch=False) is None


def test_trap_fast_path_passes_allow_fetch_false(monkeypatch):
    from app.services import trap

    seen: list[bool] = []

    def _latest(code, *, allow_fetch=True):
        seen.append(allow_fetch)
        return None

    monkeypatch.setattr(trap.financials, "latest", _latest)
    signal = trap._check_fundamental("600519", date(2026, 10, 30), [], allow_fetch=False)
    assert seen == [False]
    assert "取不到" in signal.evidence
