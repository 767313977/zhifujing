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
from app.services.limit_funnel import load_funnel_inputs, load_funnel_window_inputs

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


def test_code_details_are_opt_in_audit_fields(session):
    """R3-P2-02：代码级诊断只在审计模式暴露，默认 API 响应不变。"""
    default = build(session)
    assert all("sample_codes" not in row for row in default["series"])
    detailed = build(session, include_code_details=True)
    rows = [row for row in detailed["series"] if row["sample_count"] > 0]
    assert rows
    for row in rows:
        assert len(row["sample_codes"]) == row["sample_count"]
        assert set(row["valid_codes"]) | set(row["missing_codes"]) == set(row["sample_codes"])
        assert set(row["valid_codes"]) & set(row["missing_codes"]) == set()


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


# ---------------------------------------------------------------- S2-A
#
# 下面两组用例先锁住 S2-P1-01 / S2-P1-02 的真实问题：
# 1) 再涨停分子可以包含「没有 D+1 日线」的候选，分母却只算有效日线 → 比例 >100%；
# 2) 多日汇总把「每日均值再平均」「每日中位数的中位数」当成全体样本口径。


def _make_engine():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return engine


def _add_day(session, day: date, *, up: dict[str, int], broken: list[str] | None = None):
    for code, consecutive in up.items():
        session.add(
            LimitPool(trade_date=day, code=code, pool_type="up", consecutive=consecutive)
        )
    for code in broken or []:
        session.add(LimitPool(trade_date=day, code=code, pool_type="broken", consecutive=1))
    session.add(CollectLog(trade_date=day, task="pool_up", status="ok", rows=len(up)))
    session.add(
        CollectLog(
            trade_date=day, task="pool_broken", status="ok", rows=len(broken or [])
        )
    )


def _daily(session, day: date, code: str, pct: float, *, close: float = 10.0):
    session.add(
        StockDaily(trade_date=day, code=code, open=close, close=close, pct_chg=pct)
    )


def test_limit_up_again_uses_valid_sample_denominator():
    """S2-P1-01：缺 D+1 日线但命中 D+1 涨停池的股票不能进入分子。"""
    engine = _make_engine()
    with Session(engine) as db:
        days = [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)]
        db.add_all([TradeCalendar(trade_date=day) for day in days])
        _add_day(db, days[0], up={"A": 1, "B": 1, "C": 1})
        _add_day(db, days[1], up={"B": 1, "C": 1})
        _add_day(db, days[2], up={})
        # D+1 只有 A、B 有日线；C 停牌/缺行，但它在 D+1 涨停池里
        _daily(db, days[1], "A", 5.0)
        _daily(db, days[1], "B", 10.0)
        db.add(
            MarketSentiment(
                trade_date=days[0],
                limit_up_count=50,
                broken_rate=20.0,
                max_consecutive=3,
                up_count=3000,
                down_count=2000,
            )
        )
        db.commit()

        data = snd.build_next_day(
            db,
            days=3,
            group=snd.GROUP_ALL_LIMIT,
            metric="limit_up_count",
            include_pending=True,
            end_date=days[2],
        )
        row = {item["sample_date"]: item for item in data["series"]}[days[0]]

        assert row["sample_count"] == 3
        assert row["valid_count"] == 2
        assert row["missing_count"] == 1
        # 只有 B（有日线且再涨停）进入分子；C 缺行情不能进分子
        assert row["limit_up_again_count"] == 1
        assert row["not_limit_up_count"] == 1
        assert row["limit_up_again_ratio"] == 50.0


def test_limit_up_again_extreme_case_never_exceeds_100():
    """极端场景：候选 2 只、只有 A 有行情，D+1 涨停池含 A、B。"""
    engine = _make_engine()
    with Session(engine) as db:
        days = [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)]
        db.add_all([TradeCalendar(trade_date=day) for day in days])
        _add_day(db, days[0], up={"A": 1, "B": 1})
        _add_day(db, days[1], up={"A": 1, "B": 1})
        _add_day(db, days[2], up={})
        _daily(db, days[1], "A", 10.0)
        db.commit()

        data = snd.build_next_day(
            db,
            days=3,
            group=snd.GROUP_ALL_LIMIT,
            metric="limit_up_count",
            include_pending=True,
            end_date=days[2],
        )
        row = {item["sample_date"]: item for item in data["series"]}[days[0]]

        assert row["valid_count"] == 1
        assert row["limit_up_again_count"] == 1
        assert row["not_limit_up_count"] == 0
        assert row["limit_up_again_ratio"] == 100.0


def test_summary_keeps_sample_conservation_for_every_row(session):
    """不变量：sample = valid + missing，again/not_again 都落在有效样本内。"""
    for group in (
        snd.GROUP_ALL_LIMIT,
        snd.GROUP_FIRST,
        snd.GROUP_BROKEN,
    ):
        data = build(session, group=group)
        for row in data["series"]:
            assert row["sample_count"] == row["valid_count"] + row["missing_count"]
            if row["limit_up_again_count"] is not None:
                assert 0 <= row["limit_up_again_count"] <= row["valid_count"]
                assert 0 <= row["not_limit_up_count"] <= row["valid_count"]
                assert (
                    row["limit_up_again_count"] + row["not_limit_up_count"]
                    == row["valid_count"]
                )
            if row["limit_up_again_ratio"] is not None:
                assert 0 <= row["limit_up_again_ratio"] <= 100


def test_summary_uses_pooled_security_observations_not_day_averages():
    """S2-P1-02：D1 一只 +10%、D2 九只各 −10% → 日等权 0%，逐券 pooled −8%。"""
    engine = _make_engine()
    with Session(engine) as db:
        days = [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)]
        db.add_all([TradeCalendar(trade_date=day) for day in days])
        _add_day(db, days[0], up={"S0": 1})
        second = {f"T{index}": 1 for index in range(9)}
        _add_day(db, days[1], up=second)
        _add_day(db, days[2], up={})
        _daily(db, days[1], "S0", 10.0)
        for code in second:
            _daily(db, days[2], code, -10.0)
        for day, count in ((days[0], 50), (days[1], 20)):
            db.add(
                MarketSentiment(
                    trade_date=day,
                    limit_up_count=count,
                    broken_rate=20.0,
                    max_consecutive=2,
                    up_count=3000,
                    down_count=2000,
                )
            )
        db.commit()

        data = snd.build_next_day(
            db,
            days=3,
            group=snd.GROUP_ALL_LIMIT,
            metric="max_consecutive",
            include_pending=True,
            end_date=days[2],
        )
        summary = data["summary"]

        assert summary["valid_count"] == 10
        assert summary["stat_day_count"] == 2
        assert summary["security_weighted_avg_pct"] == -8.0
        assert summary["day_equal_weight_avg_pct"] == 0.0
        # `avg_pct` 的既有语义是「全体有效样本平均」，必须输出 pooled 值
        assert summary["avg_pct"] == -8.0
        # 中位数由 10 个观测的集合计算：九个 −10、一个 +10
        assert summary["median_pct"] == -10.0

        # 两天的 max_consecutive 都是 2 → 落在同一档位，档位表同样用 pooled 口径
        band = {item["label"]: item for item in data["bands"]}["≤2 板"]
        assert band["valid_count"] == 10
        assert band["security_weighted_avg_pct"] == -8.0
        assert band["day_equal_weight_avg_pct"] == 0.0
        assert band["avg_pct"] == -8.0
        assert band["median_pct"] == -10.0


def test_non_finite_pct_is_missing_not_zero():
    """异常涨跌幅（NaN）不能被当成 0% 计入均值与上涨/下跌家数。"""
    engine = _make_engine()
    with Session(engine) as db:
        days = [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)]
        db.add_all([TradeCalendar(trade_date=day) for day in days])
        _add_day(db, days[0], up={"A": 1, "B": 1})
        _add_day(db, days[1], up={"A": 1})
        _add_day(db, days[2], up={})
        _daily(db, days[1], "A", 5.0)
        db.add(
            StockDaily(
                trade_date=days[1],
                code="B",
                open=10.0,
                close=10.0,
                pct_chg=float("nan"),
            )
        )
        db.commit()

        data = snd.build_next_day(
            db,
            days=3,
            group=snd.GROUP_ALL_LIMIT,
            metric="limit_up_count",
            include_pending=True,
            end_date=days[2],
        )
        row = {item["sample_date"]: item for item in data["series"]}[days[0]]

        assert row["valid_count"] == 1
        assert row["missing_count"] == 1
        assert row["outcome_flat_count"] == 0
        assert row["avg_pct"] == 5.0


# ---------------------------------------------------------------- S3-B：批量断板


def test_window_loader_matches_per_day_loader(session):
    """批量窗口输入必须与逐日输入完全等价（池 / 日线 / 质量 / 题材）。"""
    calendar = [D1, D2, D3, D4]
    window = load_funnel_window_inputs(session, [D2, D3, D4], calendar)
    position = {day: index for index, day in enumerate(calendar)}
    for day in (D2, D3, D4):
        prev = calendar[position[day] - 1]
        direct = load_funnel_inputs(session, prev, day)
        assert window[day] == direct


def test_window_loader_query_budget_for_250_days():
    engine = _make_engine()
    with Session(engine) as db:
        start = date(2025, 1, 1)
        days = [date.fromordinal(start.toordinal() + index) for index in range(250)]
        db.add_all([TradeCalendar(trade_date=day) for day in days])
        for day in days:
            _add_day(db, day, up={"600000": 2, "600001": 1}, broken=["600002"])
            _daily(db, day, "600000", 1.0)
            _daily(db, day, "600001", -1.0)
            _daily(db, day, "600002", 3.0)
        db.commit()

        from sqlalchemy import event

        counter = {"n": 0}

        def before(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
            counter["n"] += 1

        event.listen(db.get_bind(), "before_cursor_execute", before)
        try:
            window = load_funnel_window_inputs(db, days, days)
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", before)

        assert len(window) == 250
        # 池 + 采集日志 + 日线（按 10 日分块）+ 基础资料 + 题材/原因 ≈ 30 条
        assert counter["n"] <= 40

        # 整条断板链路（含 build_next_day 的其他查询）也必须远低于旧 2255
        counter["n"] = 0
        event.listen(db.get_bind(), "before_cursor_execute", before)
        try:
            data = snd.build_next_day(
                db,
                days=250,
                group=snd.GROUP_FAILED,
                metric="limit_up_count",
                include_pending=True,
                end_date=days[-1],
            )
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", before)
        assert len(data["series"]) == 250
        assert counter["n"] <= 300


def test_metric_missing_is_flagged_and_not_bucketed(session):
    session.query(MarketSentiment).filter(MarketSentiment.trade_date == D1).delete()
    session.commit()
    data = build(session)
    d1 = by_date(data)[D1]
    assert "METRIC_MISSING" in d1["data_quality_reasons"]
    assert d1["sentiment_value"] is None
    assert D1.isoformat() in data["quality"]["metric_missing_days"]
    # 没有任何档位把指标缺失日当成 0 档
    assert sum(band["day_count"] for band in data["bands"]) == sum(
        1 for row in data["series"] if row["sentiment_value"] is not None
    )
