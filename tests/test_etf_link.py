"""ETF 与自选 / 个股的联动（ETF-5）。

钉这几件事：

1. 自选可以放 **ETF 代码**：名称取 `etf_basic`、行情取 `etf_bar`（零 iFinD 配额）；
2. 凡按「股票口径」处理自选的地方都要剔掉 ETF（每日同步一次 iFinD 都不该打）；
3. 简报里自选 ETF 的**份额 5 日减少 > 20%** 要提醒一行；
4. 个股 → 相关 ETF 用 ETF 名称词典归类，页面能拿到 `category` 原样显示。
"""

from contextlib import contextmanager
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api import etf as etf_api
from app.api import watchlist as watchlist_api
from app.jobs import collect_daily
from app.models import (
    AppUser,
    Base,
    EtfBar,
    EtfBasic,
    EtfCategory,
    EtfShare,
    StockBasic,
    Watchlist,
)
from app.schemas import WatchlistIn
from app.services import brief_watchlist, etf_flow, etf_screen, watch_scope

DAY = date(2026, 10, 30)


@pytest.fixture()
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'etf-link.db'}")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def db(engine):
    etf_screen._RAW_CACHE = None
    with Session(engine) as session:
        session.add(AppUser(username="admin", password_hash="x", is_admin=True))
        session.commit()
        yield session


@contextmanager
def _scope_for(engine):
    with Session(engine) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def _weekdays(size: int) -> list[date]:
    days: list[date] = []
    cursor = DAY
    while len(days) < size:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def seed_etf(db, code="510300", name="沪深300ETF", category="宽基", days=70, step=0.1):
    db.add(EtfBasic(code=code, name=name, norm_name=name.replace("ETF", ""), fund_type="股票", t0=False))
    db.add(EtfCategory(code=code, name=name, category=category))
    close = 1.0
    for day in _weekdays(days):
        prev = close
        close = round(close * (1 + step / 100), 6)
        db.add(
            EtfBar(
                trade_date=day,
                code=code,
                close=close,
                prev_close=prev,
                pct_chg=step,
                amount=5e8,
                volume=1000,
                source="tdx",
            )
        )
    return close


class _WatchStub:
    """只实现 `sync_watchlist` / `sync_stock` 用到的那几个属性。"""

    _step_note = None

    def __init__(self, calls: list[str]):
        self.calls = calls

    def sync_stock(self, code: str, days: int = 250) -> int:
        self.calls.append(code)
        return 1


def test_watchlist_accepts_etf_code(db):
    """加自选放 ETF：名字取 `etf_basic`、行情取 `etf_bar`（不是 iFinD 股票日线）。"""
    seed_etf(db)
    db.commit()
    user = db.scalars(select(AppUser)).one()

    row = watchlist_api.add_watchlist(WatchlistIn(code="510300"), user=user, session=db)

    assert row.code == "510300"
    assert row.name == "沪深300ETF"
    assert row.latest_date == DAY
    assert row.close is not None and row.pct_chg == pytest.approx(0.1)


def test_sync_watchlist_never_asks_ifind_for_etf(engine, monkeypatch):
    """每日自选同步必须把 ETF 剔掉，否则天天白烧 iFinD 配额（与转债同一个坑）。"""
    with Session(engine) as setup:
        setup.add_all(
            [
                AppUser(username="admin2", password_hash="x", is_admin=True),
                StockBasic(code="600519", name="贵州茅台"),
                EtfBasic(code="510300", name="沪深300ETF"),
                Watchlist(user_id=1, code="600519", name="贵州茅台"),
                Watchlist(user_id=1, code="510300", name="沪深300ETF"),
            ]
        )
        setup.commit()

    calls: list[str] = []
    monkeypatch.setattr(collect_daily, "session_scope", lambda: _scope_for(engine))

    written = collect_daily.DailyCollector.sync_watchlist(_WatchStub(calls))

    assert calls == ["600519"]
    assert written == 1
    assert watch_scope.is_etf_code("510300") and watch_scope.is_etf_code("159915")
    assert not watch_scope.is_etf_code("600519")
    # 任何入口直接调 sync_stock 也要在第一步挡住（不发请求）
    assert collect_daily.DailyCollector.sync_stock(_WatchStub([]), "510300") == 0


def test_brief_warns_on_etf_share_drop(db, monkeypatch):
    """自选 ETF 份额 5 日减少 > 20% → 简报多一节提醒（15:05 份额已有，日线还没有）。"""
    user = db.scalars(select(AppUser)).one()
    days = _weekdays(6)
    db.add(EtfBasic(code="510300", name="沪深300ETF", fund_type="股票", t0=False))
    db.add(Watchlist(user_id=user.id, code="510300", name="沪深300ETF"))
    shares = 1e9
    for day in days:
        db.add(EtfShare(trade_date=day, code="510300", name="沪深300ETF", shares=shares, close=2.0))
        shares *= 0.95
    db.commit()
    monkeypatch.setattr(etf_flow, "usable_days", lambda session: days)

    text = brief_watchlist.block(db, DAY)

    assert "自选 ETF 份额异动" in text
    assert "510300" in text and "近 5 日份额减少" in text


def test_related_etf_from_stock_industry(db):
    """个股 → 相关 ETF：走一级行业表，别拿 ETF 名称词典猜行业路径。

    回归两处线上实测错（2026-10-31）：
    - 「电力设备-电池-锂电池」曾被猜成电力公用（该是电池储能）；
    - 「医药生物-化学制药」曾被猜成化工材料（该是医药医疗）。
    """
    seed_etf(db, code="512480", name="半导体ETF", category="电子通信")
    seed_etf(db, code="159755", name="电池ETF", category="电池储能")
    seed_etf(db, code="512010", name="医药ETF", category="医药医疗")
    seed_etf(db, code="560280", name="工程机械ETF", category="基建制造")
    db.add_all(
        [
            StockBasic(code="300750", name="宁德时代", industry="电力设备-电池-锂电池"),
            StockBasic(code="600276", name="恒瑞医药", industry="医药生物-化学制药"),
            StockBasic(code="600519", name="贵州茅台", industry="食品饮料-白酒-白酒Ⅲ"),
            StockBasic(code="300124", name="汇川技术", industry="机械设备-自动化设备-工控设备"),
        ]
    )
    db.commit()

    battery = etf_api.related(code="300750", text=None, limit=3, session=db)
    assert battery.category == "电池储能"
    assert [row.code for row in battery.items] == ["159755"]

    medicine = etf_api.related(code="600276", text=None, limit=3, session=db)
    assert medicine.category == "医药医疗"

    # 「机械设备」只数最多（622 只），必须能归到类，否则一大批票没有卡片
    machinery = etf_api.related(code="300124", text=None, limit=3, session=db)
    assert machinery.category == "基建制造"

    # 板块 / 行业名才走名称词典；查不到就是「其他」，不硬猜
    assert etf_api.related(code=None, text="锂电池", limit=3, session=db).category == "电池储能"
    assert etf_api.related(code=None, text="综合", limit=3, session=db).items == []
