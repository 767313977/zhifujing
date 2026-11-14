"""S5-A：PIT v3 精确截止时刻（CLOSE = 15:00 Asia/Shanghai）的边界测试。

纯函数 `evaluate_known_time` 覆盖 14:59 / 15:00 / 15:01 / 15:05 / 22:00 / D+1 09:00、
UTC 与 +08:00、naive 时区约定、缺时间、来源未就绪、上游发布时间晚于 cutoff 等；
集成部分验证服务层不再把 15:00 之后的采集日志算作「当时已知」，
日终事实不冒充 15:00 已知，D+1 后验不反写。
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import (
    Base,
    CollectLog,
    LimitPool,
    MarketSentiment,
    SectorBasic,
    SectorMember,
    StockBasic,
    StockConcept,
    StockDaily,
    TradeCalendar,
)
from app.services import stock_research_context as ctx

D1 = date(2026, 11, 10)
D2 = date(2026, 11, 11)
D3 = date(2026, 11, 12)
CUTOFF = datetime(2026, 11, 11, 15, 0, tzinfo=timezone(timedelta(hours=8)))
UTC = timezone.utc
CST = timezone(timedelta(hours=8))


def evaluate(moment, **kwargs):
    return ctx.evaluate_known_time(observed_at=moment, cutoff_at=CUTOFF, **kwargs)


# ---------------------------------------------------------------- 纯函数时间边界


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 11, 11, 14, 59), ctx.KNOWN_CLOSE),
        (datetime(2026, 11, 11, 9, 25), ctx.KNOWN_CLOSE),
        (datetime(2026, 11, 11, 0, 0), ctx.KNOWN_CLOSE),
    ],
)
def test_before_cutoff_is_known(moment, expected):
    result = evaluate(moment)
    assert result["known_by"] == expected
    assert result["time_bucket"] == ctx.BUCKET_KNOWN
    assert result["reason_code"] is None


def test_exactly_cutoff_is_known_inclusive():
    result = evaluate(datetime(2026, 11, 11, 15, 0))
    assert result["known_by"] == ctx.KNOWN_CLOSE
    assert result["time_bucket"] == ctx.BUCKET_KNOWN


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 11, 11, 15, 1),
        datetime(2026, 11, 11, 15, 5),
        datetime(2026, 11, 11, 22, 0),
        datetime(2026, 11, 11, 23, 59, 59),
    ],
)
def test_after_cutoff_same_day_is_post_close_unknown(moment):
    result = evaluate(moment)
    assert result["known_by"] == ctx.KNOWN_POST_CLOSE
    assert result["time_bucket"] == ctx.BUCKET_UNKNOWN
    assert result["reason_code"] == "AFTER_CUTOFF_SAME_DAY"


def test_next_day_0900_is_later():
    result = evaluate(datetime(2026, 11, 12, 9, 0))
    assert result["known_by"] == ctx.KNOWN_LATER
    assert result["time_bucket"] == ctx.BUCKET_UNKNOWN
    assert result["reason_code"] == "AFTER_CUTOFF_LATER_DAY"


def test_utc_0659_is_1459_cst_and_known():
    result = evaluate(datetime(2026, 11, 11, 6, 59, tzinfo=UTC))
    assert result["known_by"] == ctx.KNOWN_CLOSE
    assert result["known_at"].utcoffset() == timedelta(hours=8)


def test_utc_0701_is_1501_cst_and_post_close():
    result = evaluate(datetime(2026, 11, 11, 7, 1, tzinfo=UTC))
    assert result["known_by"] == ctx.KNOWN_POST_CLOSE
    assert result["reason_code"] == "AFTER_CUTOFF_SAME_DAY"


def test_aware_cst_input_is_equivalent():
    result = evaluate(datetime(2026, 11, 11, 14, 30, tzinfo=CST))
    assert result["known_by"] == ctx.KNOWN_CLOSE


def test_naive_is_interpreted_as_cst_only_when_trusted():
    trusted = evaluate(datetime(2026, 11, 11, 14, 30), naive_timezone_trusted=True)
    assert trusted["known_by"] == ctx.KNOWN_CLOSE
    untrusted = evaluate(datetime(2026, 11, 11, 14, 30), naive_timezone_trusted=False)
    assert untrusted["known_by"] == ctx.KNOWN_UNKNOWN
    assert untrusted["reason_code"] == "NAIVE_TIMESTAMP_NO_TRUSTED_TZ"


def test_missing_observed_at_is_unknown():
    result = evaluate(None)
    assert result["known_by"] == ctx.KNOWN_UNKNOWN
    assert result["reason_code"] == "OBSERVED_AT_MISSING"
    assert result["time_bucket"] == ctx.BUCKET_UNKNOWN


def test_source_not_ready_is_unknown_even_if_early():
    result = evaluate(datetime(2026, 11, 11, 9, 0), data_ready=False)
    assert result["known_by"] == ctx.KNOWN_UNKNOWN
    assert result["reason_code"] == "SOURCE_NOT_READY"


def test_published_at_after_cutoff_demotes_early_observation():
    result = evaluate(
        datetime(2026, 11, 11, 9, 0),
        published_at=datetime(2026, 11, 11, 16, 0),
    )
    assert result["known_by"] == ctx.KNOWN_POST_CLOSE
    assert result["reason_code"] == "AFTER_CUTOFF_SAME_DAY"


def test_published_at_next_day_demotes_to_later():
    result = evaluate(
        datetime(2026, 11, 11, 9, 0),
        published_at=datetime(2026, 11, 12, 8, 0),
    )
    assert result["known_by"] == ctx.KNOWN_LATER


def test_published_at_before_cutoff_and_late_observation_uses_latest():
    result = evaluate(
        datetime(2026, 11, 11, 20, 0),
        published_at=datetime(2026, 11, 11, 10, 0),
    )
    assert result["known_by"] == ctx.KNOWN_POST_CLOSE


def test_naive_cutoff_is_rejected():
    with pytest.raises(ValueError):
        ctx.evaluate_known_time(
            observed_at=datetime(2026, 11, 11, 10, 0),
            cutoff_at=datetime(2026, 11, 11, 15, 0),
        )


def test_resolve_cutoff_at_is_explicit_and_strict():
    moment = ctx.resolve_cutoff_at(D2, "CLOSE")
    assert moment == CUTOFF
    assert moment.isoformat() == "2026-11-11T15:00:00+08:00"
    for bad in ("PREMARKET_0925", "OPEN", "POST_CLOSE"):
        with pytest.raises(ValueError):
            ctx.resolve_cutoff_at(D2, bad)


# ---------------------------------------------------------------- 集成：池来源时点


def _session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add_all([TradeCalendar(trade_date=day) for day in (D1, D2, D3)])
    db.add(StockBasic(code="600000", name="合成银行", industry="银行"))
    db.add(StockBasic(code="600009", name="合成空仓", industry="银行"))
    db.add_all(
        [
            LimitPool(trade_date=D2, code="600000", pool_type="up", name="合成银行", consecutive=2),
            LimitPool(trade_date=D2, code="600001", pool_type="up", name="合成科技", consecutive=1),
            StockDaily(trade_date=D2, code="600000", open=9.5, close=10.0, pct_chg=10.0),
            StockDaily(trade_date=D2, code="600009", open=5, close=5, pct_chg=0),
            StockDaily(trade_date=D3, code="600000", open=10.0, close=9.5, pct_chg=-5.0),
            LimitPool(trade_date=D3, code="600000", pool_type="broken", name="合成银行"),
            MarketSentiment(
                trade_date=D2,
                limit_up_count=50,
                broken_rate=20.0,
                max_consecutive=2,
                up_count=3000,
                down_count=2000,
            ),
            SectorBasic(code="801001", name="芯片", taxonomy="kph_selected"),
            SectorMember(
                trade_date=D2,
                sector_code="801001",
                code="600000",
                name="合成银行",
                fetched_at=datetime(2026, 11, 11, 9, 0),
            ),
            StockConcept(trade_date=D2, code="600000", concept="芯片"),
        ]
    )
    db.commit()
    return db


def _log(day, task, rows, moment, status="ok"):
    return CollectLog(trade_date=day, task=task, status=status, rows=rows, created_at=moment)


def _build(db, code="600000"):
    return ctx.build(db, code=code, as_of=D2)


def _pool_evidence(data):
    return [item for item in data["evidence"] if item["claim"] == "观察日池记录"][0]


@pytest.mark.parametrize(
    ("moment", "known_by", "bucket"),
    [
        (datetime(2026, 11, 11, 14, 59), ctx.KNOWN_CLOSE, ctx.BUCKET_KNOWN),
        (datetime(2026, 11, 11, 15, 0), ctx.KNOWN_CLOSE, ctx.BUCKET_KNOWN),
        (datetime(2026, 11, 11, 15, 1), ctx.KNOWN_POST_CLOSE, ctx.BUCKET_UNKNOWN),
        (datetime(2026, 11, 11, 15, 5), ctx.KNOWN_POST_CLOSE, ctx.BUCKET_UNKNOWN),
        (datetime(2026, 11, 11, 22, 0), ctx.KNOWN_POST_CLOSE, ctx.BUCKET_UNKNOWN),
        (datetime(2026, 11, 12, 9, 0), ctx.KNOWN_LATER, ctx.BUCKET_UNKNOWN),
    ],
)
def test_pool_log_time_boundaries_drive_bucket(moment, known_by, bucket):
    db = _session()
    try:
        db.add_all(
            [
                _log(D2, "pool_up", 2, moment),
                _log(D2, "pool_broken", 0, moment),
                _log(D2, "pool_down", 0, moment),
                _log(D3, "pool_up", 1, datetime(2026, 11, 12, 14, 0)),
                _log(D3, "pool_broken", 1, datetime(2026, 11, 12, 14, 0)),
                _log(D3, "pool_down", 0, datetime(2026, 11, 12, 14, 0)),
            ]
        )
        db.commit()
        data = _build(db)
        evidence = _pool_evidence(data)
        assert evidence["known_by"] == known_by
        assert evidence["time_bucket"] == bucket
        assert evidence["cutoff_at"] == "2026-11-11T15:00:00+08:00"
        expected_status = ctx.POOL_STATUS_UP if bucket == ctx.BUCKET_KNOWN else ctx.POOL_STATUS_UNKNOWN
        assert data["at_cutoff_known"]["pool_status"] == expected_status
        if bucket != ctx.BUCKET_KNOWN:
            assert "POOL_NOT_KNOWN_AT_CUTOFF" in data["quality"]["reasons"]
    finally:
        db.close()


def test_broken_failure_does_not_borrow_up_log_time():
    db = _session()
    try:
        db.add_all(
            [
                _log(D2, "pool_up", 2, datetime(2026, 11, 11, 14, 0)),
                _log(D2, "pool_broken", 0, datetime(2026, 11, 11, 14, 0), status="failed"),
                _log(D2, "pool_down", 0, datetime(2026, 11, 11, 14, 0)),
                _log(D3, "pool_up", 1, datetime(2026, 11, 12, 14, 0)),
                _log(D3, "pool_broken", 1, datetime(2026, 11, 12, 14, 0)),
                _log(D3, "pool_down", 0, datetime(2026, 11, 12, 14, 0)),
            ]
        )
        db.commit()
        data = _build(db, code="600009")
        # 空仓票需要三池都可信才能说 NOT_IN_POOL；broken 失败 → UNKNOWN
        assert data["at_cutoff_known"]["pool_status"] == ctx.POOL_STATUS_UNKNOWN
    finally:
        db.close()


def test_pool_conflict_survives_time_demotion():
    db = _session()
    try:
        db.add(LimitPool(trade_date=D2, code="600000", pool_type="broken", name="合成银行"))
        for task in ("pool_up", "pool_broken", "pool_down"):
            db.add(_log(D2, task, 1, datetime(2026, 11, 11, 22, 0)))
        for task, rows in (("pool_up", 1), ("pool_broken", 1), ("pool_down", 0)):
            db.add(_log(D3, task, rows, datetime(2026, 11, 12, 14, 0)))
        db.commit()
        data = _build(db)
        assert data["at_cutoff_known"]["pool_status"] == ctx.POOL_STATUS_CONFLICT
        assert _pool_evidence(data)["reason_code"] == "POOL_CONFLICT"
        assert _pool_evidence(data)["known_by"] == ctx.KNOWN_POST_CLOSE
    finally:
        db.close()


def test_end_of_day_facts_never_enter_known_bucket():
    db = _session()
    try:
        for task in ("pool_up", "pool_broken", "pool_down"):
            db.add(_log(D2, task, 2 if task == "pool_up" else 0, datetime(2026, 11, 11, 14, 0)))
        for task, rows in (("pool_up", 1), ("pool_broken", 1), ("pool_down", 0)):
            db.add(_log(D3, task, rows, datetime(2026, 11, 12, 14, 0)))
        db.commit()
        data = _build(db)
        assert data["at_cutoff_known"]["daily"] is None
        assert data["at_cutoff_known"]["sentiment"] is None
        assert data["end_of_day_fact"]["daily"]["pct_chg"] == 10.0
        known_known_by = {item["known_by"] for item in data["evidence"] if item["time_bucket"] == ctx.BUCKET_KNOWN}
        assert ctx.KNOWN_END_OF_DAY_FACT not in known_known_by
        assert "END_OF_DAY_FACT_NOT_AT_CUTOFF" in data["unknown_or_later"]["reasons"]
    finally:
        db.close()


def test_forward_outcome_stays_out_of_known_and_as_known():
    db = _session()
    try:
        for task in ("pool_up", "pool_broken", "pool_down"):
            db.add(_log(D2, task, 2 if task == "pool_up" else 0, datetime(2026, 11, 11, 14, 0)))
        for task, rows in (("pool_up", 1), ("pool_broken", 1), ("pool_down", 0)):
            db.add(_log(D3, task, rows, datetime(2026, 11, 12, 14, 0)))
        db.commit()
        data = _build(db)
        forward = [item for item in data["evidence"] if item["time_bucket"] == ctx.BUCKET_FORWARD]
        assert len(forward) == 1
        assert forward[0]["known_by"] == ctx.KNOWN_FORWARD
        assert forward[0]["source_trade_date"] == D3
        assert data["later_outcome"]["pct_chg"] == -5.0
        assert data["as_known_on_day"]["daily"] is None
        assert data["as_known_on_day"]["sentiment"] is None
        assert data["as_known_on_day"]["schema_version"] == "research-evidence-v3"
        # 兼容字段里不允许出现后验或日终事实标记
        flat = str(data["as_known_on_day"])
        assert ctx.KNOWN_FORWARD not in flat
        assert ctx.KNOWN_END_OF_DAY_FACT not in flat
    finally:
        db.close()


def test_naive_timezone_untrusted_demotes_pool_evidence():
    db = _session()
    try:
        for task in ("pool_up", "pool_broken", "pool_down"):
            db.add(_log(D2, task, 2 if task == "pool_up" else 0, datetime(2026, 11, 11, 14, 0)))
        db.commit()
        result = ctx.evaluate_known_time(
            observed_at=datetime(2026, 11, 11, 14, 0),
            cutoff_at=CUTOFF,
            data_ready=True,
            naive_timezone_trusted=False,
        )
        assert result["known_by"] == ctx.KNOWN_UNKNOWN
        assert result["reason_code"] == "NAIVE_TIMESTAMP_NO_TRUSTED_TZ"
    finally:
        db.close()
