"""估值分位与涨跌分布的口径：温度档、分位窗口、分档计数。

这几项都是「数字看起来对、口径其实错」的高危区，所以用合成数据钉死：
- 分位必须是**中位秩**、且样本不足 20 个点时给 None（不填 0）；
- 温度档的边界（10/30/70/90）左闭右开；
- 涨跌分布的分档区间是「左开右闭」、平盘单独一档。
"""

from datetime import date, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.market import advance_decline
from app.models import Base, IndexValuation, StockDaily
from app.services import valuation

DAY = date(2026, 10, 22)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _add_valuation(session: Session, values: list[float]) -> None:
    """按周频塞入一串 PE（PB 固定 1.5），最新一个点落在 DAY。"""
    for index, value in enumerate(values):
        day = DAY - timedelta(days=7 * (len(values) - 1 - index))
        session.add(
            IndexValuation(
                trade_date=day,
                index_code="000300.SH",
                name="沪深300",
                pe_ttm=value,
                pb=1.5,
                source="test",
            )
        )
    session.commit()


def test_tone_bands_are_left_closed():
    assert valuation.tone_of(None) is None
    assert valuation.tone_of(0) == "极冷"
    assert valuation.tone_of(10) == "偏冷"
    assert valuation.tone_of(29.9) == "偏冷"
    assert valuation.tone_of(30) == "正常"
    assert valuation.tone_of(69.9) == "正常"
    assert valuation.tone_of(70) == "偏热"
    assert valuation.tone_of(90) == "极热"


def test_summary_uses_latest_point_and_window(session):
    _add_valuation(session, [10 + index for index in range(30)])
    data = valuation.summary(session)
    item = data.items[0]
    assert item.code == "000300.SH"
    assert item.pe_ttm == 39
    # 39 是这 30 个点里最高的 → 中位秩分位接近 100
    assert item.pe_pct is not None and item.pe_pct > 95
    assert item.tone == "极热"
    assert data.market_pct == item.pe_pct
    assert data.as_of == DAY


def test_summary_needs_enough_samples(session):
    _add_valuation(session, [10, 11, 12])
    item = valuation.summary(session).items[0]
    assert item.pe_ttm == 12
    # 不足 20 个点：不给分位，也不给温度（不拿 3 个点冒充「历史分位」）
    assert item.pe_pct is None
    assert item.tone is None


def test_advance_decline_buckets(session):
    pcts = [-8.0, -6.0, -2.0, 0.0, 2.0, 8.0]
    for index, pct in enumerate(pcts):
        session.add(
            StockDaily(
                trade_date=DAY,
                code=f"60000{index}",
                close=10.0,
                pct_chg=pct,
            )
        )
    session.commit()

    data = advance_decline(trade_date=DAY, session=session)
    assert data.total == 6
    assert (data.up, data.down, data.flat) == (2, 3, 1)
    counts = {bucket.label: bucket.count for bucket in data.buckets}
    assert counts["≤-7%"] == 1
    assert counts["-7~-5%"] == 1
    assert counts["-3~-1%"] == 1
    assert counts["平盘"] == 1
    assert counts["1~3%"] == 1
    assert counts[">7%"] == 1
    # 中位数 = (-2 + 0) / 2
    assert data.median_pct == -1.0


def test_advance_decline_without_data_raises(session):
    with pytest.raises(HTTPException) as excinfo:
        advance_decline(trade_date=DAY, session=session)
    assert excinfo.value.status_code == 404
