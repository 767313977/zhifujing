"""S1-B：情绪 × 次日表现（P1-C）。

合成库只含 D 日样本与 D+1 日线，用来锁住：分组口径、D+1 由交易日历确定、
PENDING/PARTIAL/UNVERIFIED 的区分、不把缺数据写成 0%、以及断板复用 P0-B。
"""

from datetime import date

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import (
    Base,
    CollectLog,
    LimitPool,
    MarketSentiment,
    StockDaily,
    TradeCalendar,
)
from app.services import sentiment_next_day as snd

D1 = date(2026, 11, 2)
D2 = date(2026, 11, 3)
D3 = date(2026, 11, 4)
D4 = date(2026, 11, 5)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([TradeCalendar(trade_date=day) for day in (D1, D2, D3, D4)])
        db.add_all(
            [
                # D1：A 首板、B 二板、C 炸板
                LimitPool(trade_date=D1, code="A", pool_type="up", consecutive=1),
                LimitPool(trade_date=D1, code="B", pool_type="up", consecutive=2),
                LimitPool(trade_date=D1, code="C", pool_type="broken", consecutive=1),
                # D2：A 晋级二板、E 首板、D 炸板
                LimitPool(trade_date=D2, code="A", pool_type="up", consecutive=2),
                LimitPool(trade_date=D2, code="E", pool_type="up", consecutive=1),
                LimitPool(trade_date=D2, code="D", pool_type="broken", consecutive=1),
                # D3：F 首板（D4 还没有日线 → PENDING）
                LimitPool(trade_date=D3, code="F", pool_type="up", consecutive=1),
            ]
        )
        for day in (D1, D2, D3):
            db.add_all(
                [
                    CollectLog(trade_date=day, task="pool_up", status="ok", rows=2),
                    CollectLog(trade_date=day, task="pool_broken", status="ok", rows=1),
                ]
            )
        db.add_all(
            [
                MarketSentiment(
                    trade_date=D1,
                    limit_up_count=50,
                    broken_rate=30.0,
                    max_consecutive=2,
                    up_count=3000,
                    down_count=2000,
                ),
                MarketSentiment(
                    trade_date=D2,
                    limit_up_count=20,
                    broken_rate=10.0,
                    max_consecutive=2,
                    up_count=1000,
                    down_count=4000,
                ),
                MarketSentiment(
                    trade_date=D3,
                    limit_up_count=120,
                    broken_rate=40.0,
                    max_consecutive=5,
                    up_count=4000,
                    down_count=1000,
                ),
            ]
        )
        db.add_all(
            [
                # D1 收盘（用于开盘涨幅）
                StockDaily(trade_date=D1, code="A", close=10.0, pct_chg=10.0),
                StockDaily(trade_date=D1, code="B", close=20.0, pct_chg=10.0),
                StockDaily(trade_date=D1, code="C", close=5.0, pct_chg=10.0),
                # D2 结果：A +10、B -3、E +5；C 停牌（缺行）
                StockDaily(trade_date=D2, code="A", open=11.0, close=11.0, pct_chg=10.0),
                StockDaily(trade_date=D2, code="B", open=19.0, close=19.4, pct_chg=-3.0),
                StockDaily(trade_date=D2, code="E", open=10.5, close=10.5, pct_chg=5.0),
                # D3 结果：A +2、B +1、E -1
                StockDaily(trade_date=D3, code="A", open=11.0, close=11.22, pct_chg=2.0),
                StockDaily(trade_date=D3, code="B", open=19.4, close=19.6, pct_chg=1.0),
                StockDaily(trade_date=D3, code="E", open=10.5, close=10.4, pct_chg=-1.0),
            ]
        )
        db.commit()
        yield db


def build(session, **kwargs):
    params = {
        "days": 3,
        "group": snd.GROUP_FIRST,
        "metric": "limit_up_count",
        "include_pending": True,
        "end_date": D3,
    }
    params.update(kwargs)
    return snd.build_next_day(session, **params)


def by_date(data):
    return {row["sample_date"]: row for row in data["series"]}


def test_first_board_group_and_next_day_stats(session):
    data = build(session)
    rows = by_date(data)
    assert set(rows) == {D1, D2, D3}

    d1 = rows[D1]
    assert d1["outcome_date"] == D2
    assert d1["sample_count"] == 1  # 只有 A 是首板（B 是二板）
    assert d1["valid_count"] == 1
    assert d1["avg_pct"] == 10.0
    assert d1["median_pct"] == 10.0
    assert d1["up_ratio"] == 100.0
    assert d1["limit_up_again_count"] == 1
    assert d1["limit_up_again_ratio"] == 100.0
    assert d1["data_quality_status"] == snd.STATUS_VERIFIED
    assert d1["data_as_of"] == D3

    d2 = rows[D2]
    assert d2["sample_count"] == 1  # E
    assert d2["avg_pct"] == -1.0
    assert d2["up_ratio"] == 0.0
    assert d2["limit_up_again_count"] == 0

    # D3 的 D+1（D4）没有日线 → PENDING，不是 0%
    d3 = rows[D3]
    assert d3["data_quality_status"] == snd.STATUS_PENDING
    assert d3["avg_pct"] is None
    assert d3["up_ratio"] is None
    assert d3["limit_up_again_ratio"] is None

    assert data["summary"]["stat_day_count"] == 2
    assert data["summary"]["sample_count"] == 2
    assert data["summary"]["avg_pct"] == 4.5
    assert data["summary"]["up_ratio"] == 50.0
    assert data["summary"]["limit_up_again_ratio"] == 50.0
    assert data["quality"]["pending_days"] == [D3.isoformat()]


def test_all_limit_up_includes_second_board(session):
    data = build(session, group=snd.GROUP_ALL_LIMIT)
    d1 = by_date(data)[D1]
    assert d1["sample_count"] == 2
    assert d1["valid_count"] == 2
    assert d1["avg_pct"] == 3.5
    assert d1["up_ratio"] == 50.0
    assert d1["limit_up_again_count"] == 1


def test_broken_group_reports_missing_sample_not_zero(session):
    data = build(session, group=snd.GROUP_BROKEN)
    d1 = by_date(data)[D1]
    assert d1["sample_count"] == 1  # C
    assert d1["valid_count"] == 0
    assert d1["missing_count"] == 1
    assert d1["avg_pct"] is None
    assert d1["data_quality_status"] == snd.STATUS_PARTIAL
    assert "OUTCOME_PARTIAL" in d1["data_quality_reasons"]


def test_failed_promotion_reuses_p0b_classification(session):
    data = build(session, group=snd.GROUP_FAILED)
    rows = by_date(data)
    # D2 的断板 = D1 涨停里今日既没涨停也没炸板、且有 D2 日线的 → B
    d2 = rows[D2]
    assert d2["sample_count"] == 1
    assert d2["outcome_date"] == D3
    assert d2["avg_pct"] == 1.0
    assert d2["data_quality_status"] == snd.STATUS_VERIFIED


def test_include_pending_false_filters_pending_rows(session):
    data = build(session, include_pending=False)
    assert all(row["data_quality_status"] != snd.STATUS_PENDING for row in data["series"])
    assert D3 not in by_date(data)
    assert data["quality"]["pending_days"] == [D3.isoformat()]


def test_day_without_up_pool_log_is_unverified(session):
    session.query(CollectLog).filter(
        CollectLog.trade_date == D1, CollectLog.task == "pool_up"
    ).delete()
    session.commit()
    data = build(session)
    d1 = by_date(data)[D1]
    assert d1["sample_count"] == 0
    assert d1["data_quality_status"] == snd.STATUS_UNVERIFIED
    assert "UP_POOL_MISSING" in d1["data_quality_reasons"]
    assert d1["avg_pct"] is None


def test_empty_up_pool_with_ok_log_is_not_trusted(session):
    # 把 D1 的涨停池改成 0 行但日志仍是 ok（来源空表异常）
    session.query(LimitPool).filter(
        LimitPool.trade_date == D1, LimitPool.pool_type == "up"
    ).delete()
    session.commit()
    data = build(session)
    d1 = by_date(data)[D1]
    assert d1["data_quality_status"] == snd.STATUS_UNVERIFIED
    assert "UP_POOL_EMPTY" in d1["data_quality_reasons"]


def test_sentiment_bands_use_fixed_thresholds(session):
    data = build(session)
    labels = [band["label"] for band in data["bands"]]
    assert labels == ["<30 家", "30–60 家", "60–100 家", "≥100 家"]
    assert data["threshold_version"] == snd.THRESHOLD_VERSION

    bands = {band["label"]: band for band in data["bands"]}
    # D1=50 家 → 30–60；D2=20 家 → <30；D3=120 家（PENDING，无有效统计）
    assert bands["30–60 家"]["day_count"] == 1
    assert bands["30–60 家"]["avg_pct"] == 10.0
    assert bands["<30 家"]["day_count"] == 1
    assert bands["<30 家"]["avg_pct"] == -1.0
    assert bands["≥100 家"]["day_count"] == 1
    assert bands["≥100 家"]["valid_count"] == 0
    assert bands["≥100 家"]["avg_pct"] is None


def test_advance_ratio_metric(session):
    data = build(session, metric="advance_ratio")
    d1 = by_date(data)[D1]
    assert d1["sentiment_value"] == 60.0
    assert data["metric_label"] == "上涨家数占比"


def test_samples_come_from_d_day_pool_not_future(session):
    """D2 的 E 在 D1 不是首板，不能被算进 D1 样本（禁止未来反向筛选）。"""
    data = build(session)
    d1 = by_date(data)[D1]
    assert d1["sample_count"] == 1


def test_invalid_group_or_metric_raises(session):
    with pytest.raises(ValueError):
        build(session, group="nope")
    with pytest.raises(ValueError):
        build(session, metric="nope")


def test_build_is_read_only(session):
    before = (
        session.scalar(select(func.count()).select_from(StockDaily)),
        session.scalar(select(func.count()).select_from(LimitPool)),
        session.scalar(select(func.count()).select_from(MarketSentiment)),
        session.scalar(select(func.count()).select_from(CollectLog)),
    )
    build(session)
    after = (
        session.scalar(select(func.count()).select_from(StockDaily)),
        session.scalar(select(func.count()).select_from(LimitPool)),
        session.scalar(select(func.count()).select_from(MarketSentiment)),
        session.scalar(select(func.count()).select_from(CollectLog)),
    )
    assert before == after
