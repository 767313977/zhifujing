"""ETF 筛选（ETF-2）的四条口径。

1. 涨幅按 `pct_chg` **复利**，不是 `close` 相除（分红日不能算成大跌）；
2. 同指数去重：每组只留 20 日均额最大的那只，其余折叠成「同类 N 只」；
3. `t0` 预设只留 T+0 品种，并**排除 QDII**（海外股票）；
4. 动量预设只收股票类、规模与均额达标、且站上 MA20 的。
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, EtfBar, EtfBasic, EtfCategory, EtfShare
from app.services import etf_screen

END = date(2026, 10, 30)
DAYS = 70


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'etf-screen.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _dates(size: int = DAYS) -> list[date]:
    days: list[date] = []
    cursor = END
    while len(days) < size:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def seed(
    db,
    code,
    name,
    norm,
    category,
    fund_type,
    t0=False,
    amount=1e8,
    shares=1e9,
    step=0.1,
):
    db.add(EtfBasic(code=code, name=name, norm_name=norm, fund_type=fund_type, t0=t0))
    db.add(EtfCategory(code=code, name=name, category=category))
    close = 2.0
    for day in _dates():
        close = round(close * (1 + step / 100), 4)
        db.add(
            EtfBar(
                trade_date=day,
                code=code,
                close=close,
                prev_close=round(close / (1 + step / 100), 4),
                pct_chg=step,
                amount=amount,
                volume=1000,
                source="tdx",
            )
        )
    for day in _dates(25):
        db.add(EtfShare(trade_date=day, code=code, shares=shares, close=close))


def test_compound_uses_pct_series():
    # 分红日 close 会跳一下，但 pct_chg 是调整后的：乘起来才是真实收益
    assert etf_screen._compound([10.0, -50.0, 60.0], 3) == pytest.approx(-12.0, abs=0.01)
    # 先涨 10% 再跌 50% → 1.1×0.5−1 = −45%
    assert etf_screen._compound([10.0, -50.0], 2) == pytest.approx(-45.0, abs=0.01)


def test_dedup_keeps_largest_avg_amount(db):
    seed(db, "510300", "沪深300ETF华泰柏瑞", "沪深300", "宽基", "股票", amount=5e8)
    seed(db, "510310", "沪深300ETF易方达", "沪深300", "宽基", "股票", amount=1e8)
    db.commit()

    data = etf_screen.screen(db, END, preset="momentum")

    by_code = {row.code: row for row in data.items}
    assert "510300" in by_code
    assert "510310" not in by_code
    assert by_code["510300"].same_count == 2
    assert data.total == 1


def test_t0_preset_excludes_qdii(db):
    seed(db, "518880", "黄金ETF华安", "黄金", "黄金贵金属", "商品", t0=True)
    seed(db, "513100", "纳指ETF国泰", "纳指", "跨境宽基", "QDII", t0=True)
    seed(db, "510300", "沪深300ETF", "沪深300", "宽基", "股票", t0=False)
    db.commit()

    data = etf_screen.screen(db, END, preset="t0")

    assert [row.code for row in data.items] == ["518880"]
    assert data.filters["exclude_qdii"] is True


def test_momentum_requires_stock_type_and_ma20(db):
    seed(db, "510300", "沪深300ETF", "沪深300", "宽基", "股票", amount=5e8, shares=1e10)
    seed(db, "511010", "国债ETF国泰", "国债", "债券", "债券", amount=5e8, shares=1e10)
    # 一路下跌的股票 ETF：站不上 MA20，动量预设要剔掉
    seed(db, "512000", "券商ETF", "券商", "证券保险", "股票", amount=5e8, shares=1e10, step=-0.5)
    db.commit()

    data = etf_screen.screen(db, END, preset="momentum")

    assert [row.code for row in data.items] == ["510300"]
    assert data.filters["fund_type"] == "股票"


def test_detail_opens_non_stock_etf_and_lists_peers(db):
    # 货币 ETF 不能被动量预设挡住（详情不套门槛）
    seed(db, "511990", "华宝添益ETF", "华宝添益", "货币现金", "货币", t0=True)
    seed(db, "510300", "沪深300ETF华泰柏瑞", "沪深300", "宽基", "股票", amount=5e8)
    seed(db, "510310", "沪深300ETF易方达", "沪深300", "宽基", "股票", amount=1e8)
    db.commit()

    money = etf_screen.detail(db, "511990", END)
    broad = etf_screen.detail(db, "510300", END)

    assert money is not None and money.row.code == "511990"
    assert len(money.series) == DAYS
    assert broad is not None
    assert [peer["code"] for peer in broad.peers] == ["510310"]


def test_scale_and_flow_are_none_when_missing(db):
    """没有份额快照时规模 / 净申赎记 None（不拿 0 顶）。"""
    db.add(EtfBasic(code="510500", name="中证500ETF", norm_name="中证500", fund_type="股票"))
    db.add(EtfCategory(code="510500", name="中证500ETF", category="宽基"))
    close = 2.0
    for day in _dates():
        close = round(close * 1.001, 4)
        db.add(
            EtfBar(
                trade_date=day,
                code="510500",
                close=close,
                prev_close=round(close / 1.001, 4),
                pct_chg=0.1,
                amount=5e8,
                volume=1000,
                source="tdx",
            )
        )
    db.commit()

    # 把动量预设的门槛关掉，专测「缺份额时给 None」
    data = etf_screen.screen(
        db,
        END,
        preset="momentum",
        dedup=False,
        scale_min=0,
        amount_min=0,
        above_ma20=False,
    )

    row = data.items[0]
    assert row.scale is None
    assert row.flow20 is None
    assert row.flow20_ratio is None
