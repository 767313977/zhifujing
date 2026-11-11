"""晋级漏斗：口径、质量证据与旧接口对账（全部离线合成数据）。"""

from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.deps import resolve_trade_date
from app.api.limit import _parse_funnel_levels, promotion, promotion_funnel, promotion_funnel_dates
from app.models import (
    Base,
    CollectLog,
    LimitPool,
    StockDaily,
    TradeCalendar,
)
from app.services.limit_funnel import (
    BROKEN,
    FAILED,
    NOT_COLLECTED,
    PARTIAL,
    PROMOTED,
    SOURCE_FAILED,
    SOURCE_NOT_COLLECTED,
    SOURCE_OK,
    SOURCE_UNKNOWN,
    UNVERIFIED,
    VERIFIED,
    FunnelInputs,
    SourceQuality,
    build_promotion_funnel,
    list_funnel_dates,
    load_funnel_inputs,
    validate_funnel_invariants,
)

PREV = date(2026, 9, 23)
DAY = date(2026, 9, 24)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'funnel.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([TradeCalendar(trade_date=PREV), TradeCalendar(trade_date=DAY)])
        session.commit()
        yield session


def q(status=SOURCE_OK, rows=0):
    return SourceQuality(status, rows)


def inputs(
    *,
    prev=None,
    up=None,
    broken=None,
    daily=None,
    prev_q=None,
    up_q=None,
    broken_q=None,
):
    return FunnelInputs(
        trade_date=DAY,
        prev_date=PREV,
        prev_up=prev or {},
        today_up=up or {},
        today_broken=broken or {},
        today_daily=daily or {},
        prev_up_quality=prev_q or q(rows=len(prev or {})),
        today_up_quality=up_q or q(rows=len(up or {})),
        today_broken_quality=broken_q or q(rows=len(broken or {})),
    )


def level_of(levels, level):
    return next(item for item in levels if item["level"] == level)


def stock_of(levels, code):
    for item in levels:
        for stock in item["stocks"]:
            if stock["code"] == code:
                return stock
    raise AssertionError(code)


def test_first_board_promoted():
    levels, quality = build_promotion_funnel(
        inputs(prev={"A": {"consecutive": 1}}, up={"A": {"consecutive": 2}}, daily={"A": {}}),
        [1],
    )
    assert quality.status == VERIFIED
    row = level_of(levels, 1)
    assert (row["base"], row["promoted"], row["broken"], row["failed"], row["unverifiable"]) == (
        1,
        1,
        0,
        0,
        0,
    )
    assert row["legacy_rate"] == 100.0 and row["verified_rate"] == 100.0
    assert stock_of(levels, "A")["status"] == PROMOTED


def test_second_board_promoted():
    levels, _ = build_promotion_funnel(
        inputs(prev={"A": {"consecutive": 2}}, up={"A": {"consecutive": 3}}, daily={"A": {}}),
        [1, 2],
    )
    assert level_of(levels, 1)["base"] == 0
    assert level_of(levels, 2)["promoted"] == 1


def test_broken_is_not_promoted():
    levels, _ = build_promotion_funnel(
        inputs(prev={"A": {"consecutive": 1}}, broken={"A": {}}, daily={"A": {}}), [1]
    )
    assert level_of(levels, 1)["broken"] == 1
    assert stock_of(levels, "A")["status"] == BROKEN


def test_quote_but_neither_pool_is_failed():
    levels, _ = build_promotion_funnel(
        inputs(prev={"A": {"consecutive": 1}}, daily={"A": {"close": 10.0}}), [1]
    )
    assert level_of(levels, 1)["failed"] == 1
    assert stock_of(levels, "A")["status"] == FAILED
    assert stock_of(levels, "A")["close"] == 10.0


def test_daily_missing_is_unverifiable():
    levels, quality = build_promotion_funnel(
        inputs(prev={"A": {"consecutive": 1}}), [1]
    )
    assert level_of(levels, 1)["unverifiable"] == 1
    assert stock_of(levels, "A")["reason_code"] == "DAILY_QUOTE_MISSING"
    assert quality.status == PARTIAL


def test_up_source_failed_is_unverifiable_even_with_row():
    levels, quality = build_promotion_funnel(
        inputs(
            prev={"A": {"consecutive": 1}},
            up={"A": {"consecutive": 2}},
            daily={"A": {}},
            up_q=q(SOURCE_FAILED),
        ),
        [1],
    )
    assert quality.status == UNVERIFIED
    assert stock_of(levels, "A")["reason_code"] == "COLLECTION_FAILED"


def test_broken_source_failed_blocks_failed_classification():
    levels, quality = build_promotion_funnel(
        inputs(
            prev={"A": {"consecutive": 1}},
            daily={"A": {}},
            broken_q=q(SOURCE_FAILED),
        ),
        [1],
    )
    assert quality.status == PARTIAL
    assert stock_of(levels, "A")["reason_code"] == "COLLECTION_FAILED"
    assert level_of(levels, 1)["failed"] == 0


def test_prev_pool_missing_returns_no_levels():
    levels, quality = build_promotion_funnel(
        inputs(prev={}, prev_q=q(SOURCE_NOT_COLLECTED)), [1, 2]
    )
    assert levels == []
    assert quality.status == NOT_COLLECTED
    assert "PREVIOUS_UP_POOL_MISSING" in quality.reasons


def test_prev_pool_unknown_with_rows_keeps_observed_rows_unverified():
    levels, quality = build_promotion_funnel(
        inputs(
            prev={"A": {"consecutive": 1}},
            up={"A": {"consecutive": 2}},
            daily={"A": {}},
            prev_q=q(SOURCE_UNKNOWN, 1),
        ),
        [1],
    )
    assert quality.status == UNVERIFIED
    assert level_of(levels, 1)["base"] == 1
    assert level_of(levels, 1)["unverifiable"] == 1
    assert level_of(levels, 1)["verified_rate"] is None
    assert stock_of(levels, "A")["reason_code"] == "PREVIOUS_UP_POOL_UNVERIFIED"


def test_consecutive_mismatch_is_unverifiable_not_failed():
    levels, _ = build_promotion_funnel(
        inputs(
            prev={"A": {"consecutive": 1}},
            up={"A": {"consecutive": 3}},
            daily={"A": {}},
        ),
        [1],
    )
    assert level_of(levels, 1)["failed"] == 0
    assert stock_of(levels, "A")["reason_code"] == "CONSECUTIVE_MISMATCH"


def test_pool_conflict_is_unverifiable_and_not_double_counted():
    levels, _ = build_promotion_funnel(
        inputs(
            prev={"A": {"consecutive": 1}},
            up={"A": {"consecutive": 2}},
            broken={"A": {}},
            daily={"A": {}},
        ),
        [1],
    )
    row = level_of(levels, 1)
    assert (row["base"], row["promoted"], row["broken"], row["unverifiable"]) == (1, 0, 0, 1)
    assert stock_of(levels, "A")["reason_code"] == "POOL_CONFLICT"


def test_base_zero_rates_are_null():
    levels, quality = build_promotion_funnel(inputs(prev={}, prev_q=q(rows=0)), [1, 2])
    assert quality.status == VERIFIED
    for level in levels:
        assert level["base"] == 0
        assert level["legacy_rate"] is None and level["verified_rate"] is None


def test_all_unverifiable_has_null_verified_rate():
    levels, quality = build_promotion_funnel(
        inputs(
            prev={"A": {"consecutive": 1}, "B": {"consecutive": 1}},
            up_q=q(SOURCE_FAILED),
        ),
        [1],
    )
    row = level_of(levels, 1)
    assert row["unverifiable"] == 2 and row["valid_base"] == 0
    assert row["verified_rate"] is None
    assert quality.status == UNVERIFIED


def test_only_promoted():
    levels, _ = build_promotion_funnel(
        inputs(
            prev={"A": {"consecutive": 1}, "B": {"consecutive": 1}},
            up={"A": {"consecutive": 2}, "B": {"consecutive": 2}},
            daily={"A": {}, "B": {}},
        ),
        [1],
    )
    row = level_of(levels, 1)
    assert (row["promoted"], row["broken"], row["failed"], row["unverifiable"]) == (2, 0, 0, 0)


def test_mixed_buckets():
    levels, _ = build_promotion_funnel(
        inputs(
            prev={
                "A": {"consecutive": 1},
                "B": {"consecutive": 1},
                "C": {"consecutive": 1},
                "D": {"consecutive": 1},
            },
            up={"A": {"consecutive": 2}},
            broken={"B": {}},
            daily={"A": {}, "B": {}, "C": {}},
        ),
        [1],
    )
    row = level_of(levels, 1)
    assert (row["promoted"], row["broken"], row["failed"], row["unverifiable"]) == (1, 1, 1, 1)
    assert row["base"] == 4 and row["valid_base"] == 3
    validate_funnel_invariants(levels)


def test_codes_are_unique_and_levels_do_not_mix():
    levels, _ = build_promotion_funnel(
        inputs(
            prev={"A": {"consecutive": 1}, "B": {"consecutive": 2}},
            up={"A": {"consecutive": 2}, "B": {"consecutive": 3}},
            daily={"A": {}, "B": {}},
        ),
        [1, 2],
    )
    assert level_of(levels, 1)["base"] == 1
    assert level_of(levels, 2)["base"] == 1
    for row in levels:
        codes = [stock["code"] for stock in row["stocks"]]
        assert len(codes) == len(set(codes))


def test_load_inputs_latest_log_wins_after_retry(db):
    rows = [
        LimitPool(trade_date=PREV, code="A", pool_type="up", consecutive=1),
        LimitPool(trade_date=DAY, code="A", pool_type="up", consecutive=2),
        StockDaily(trade_date=DAY, code="A", close=10.0, pct_chg=10.0),
    ]
    db.add_all(rows)
    db.add_all(
        [
            CollectLog(trade_date=PREV, task="pool_up", status="ok", rows=1),
            CollectLog(trade_date=DAY, task="pool_up", status="failed", rows=0),
            CollectLog(trade_date=DAY, task="pool_up", status="ok", rows=1),
            CollectLog(trade_date=DAY, task="pool_broken", status="ok", rows=0),
        ]
    )
    db.commit()
    data = load_funnel_inputs(db, PREV, DAY)
    assert data.today_up_quality.status == SOURCE_OK
    levels, quality = build_promotion_funnel(data, [1])
    assert quality.status == VERIFIED
    assert level_of(levels, 1)["promoted"] == 1


def test_old_rows_without_logs_are_unknown(db):
    db.add_all(
        [
            LimitPool(trade_date=PREV, code="A", pool_type="up", consecutive=1),
            LimitPool(trade_date=DAY, code="A", pool_type="up", consecutive=2),
            StockDaily(trade_date=DAY, code="A", close=10.0),
        ]
    )
    db.commit()
    data = load_funnel_inputs(db, PREV, DAY)
    levels, quality = build_promotion_funnel(data, [1])
    assert quality.status == UNVERIFIED
    assert level_of(levels, 1)["base"] == 1
    assert level_of(levels, 1)["unverifiable"] == 1


def test_null_close_is_not_a_valid_quote(db):
    db.add_all(
        [
            LimitPool(trade_date=PREV, code="A", pool_type="up", consecutive=1),
            StockDaily(trade_date=DAY, code="A", close=None),
            CollectLog(trade_date=PREV, task="pool_up", status="ok", rows=1),
            CollectLog(trade_date=DAY, task="pool_up", status="ok", rows=1),
            CollectLog(trade_date=DAY, task="pool_broken", status="ok", rows=0),
        ]
    )
    db.commit()
    data = load_funnel_inputs(db, PREV, DAY)
    levels, quality = build_promotion_funnel(data, [1])
    assert quality.status == PARTIAL
    assert stock_of(levels, "A")["reason_code"] == "DAILY_QUOTE_MISSING"


def test_list_dates_lightweight_quality(db):
    db.add_all(
        [
            LimitPool(trade_date=PREV, code="A", pool_type="up", consecutive=1),
            LimitPool(trade_date=DAY, code="A", pool_type="up", consecutive=2),
            CollectLog(trade_date=PREV, task="pool_up", status="ok", rows=1),
            CollectLog(trade_date=DAY, task="pool_up", status="ok", rows=1),
            CollectLog(trade_date=DAY, task="pool_broken", status="ok", rows=0),
        ]
    )
    db.commit()
    items = list_funnel_dates(db, 250)
    item = next(row for row in items if row["trade_date"] == DAY)
    assert item["prev_date"] == PREV
    assert item["quality_status"] == VERIFIED
    assert item["can_calculate_verified_rate"] is True


def test_api_rejects_bad_levels():
    with pytest.raises(HTTPException) as exc:
        _parse_funnel_levels("1,a")
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException):
        _parse_funnel_levels("0,1")
    with pytest.raises(HTTPException):
        _parse_funnel_levels("11")
    assert _parse_funnel_levels("2,1,2") == [1, 2]


def test_api_rejects_future_and_non_trade_date(db):
    with pytest.raises(HTTPException) as future:
        resolve_trade_date(date(2099, 1, 1), db)
    assert future.value.status_code == 404
    with pytest.raises(HTTPException):
        resolve_trade_date(date(2026, 10, 31), db)


def test_api_no_previous_trade_day(db):
    db.query(TradeCalendar).filter(TradeCalendar.trade_date == PREV).delete()
    db.commit()
    out = promotion_funnel(trade_date=DAY, levels="1", include_stocks=True, session=db)
    # 日历里只有 DAY 本身，没有上一交易日
    assert out.prev_date is None
    assert out.levels == []
    assert out.quality.status == UNVERIFIED
    assert "NO_PREVIOUS_TRADE_DAY" in out.quality.reasons


def test_api_include_stocks_false_strips_rows(db):
    db.add_all(
        [
            LimitPool(trade_date=PREV, code="A", pool_type="up", consecutive=1),
            LimitPool(trade_date=DAY, code="A", pool_type="up", consecutive=2),
            StockDaily(trade_date=DAY, code="A", close=10.0),
            CollectLog(trade_date=PREV, task="pool_up", status="ok", rows=1),
            CollectLog(trade_date=DAY, task="pool_up", status="ok", rows=1),
            CollectLog(trade_date=DAY, task="pool_broken", status="ok", rows=0),
        ]
    )
    db.commit()
    out = promotion_funnel(trade_date=DAY, levels="1", include_stocks=False, session=db)
    assert out.levels[0].base == 1
    assert out.levels[0].stocks == []
    out = promotion_funnel(trade_date=DAY, levels="1", include_stocks=True, session=db)
    assert len(out.levels[0].stocks) == 1


def test_api_dates_endpoint(db):
    db.add_all(
        [
            LimitPool(trade_date=PREV, code="A", pool_type="up", consecutive=1),
            LimitPool(trade_date=DAY, code="A", pool_type="up", consecutive=2),
            CollectLog(trade_date=PREV, task="pool_up", status="ok", rows=1),
            CollectLog(trade_date=DAY, task="pool_up", status="ok", rows=1),
            CollectLog(trade_date=DAY, task="pool_broken", status="ok", rows=0),
        ]
    )
    db.commit()
    out = promotion_funnel_dates(days=250, session=db)
    assert out.dates
    assert any(item.trade_date == DAY for item in out.dates)


def test_legacy_api_unchanged_and_reconciles(db):
    # 三天：D-2 / D-1 / D，D-1 两板 A→D 三板；D-1 首板 B→D 两板
    d2 = date(2026, 9, 22)
    db.add(TradeCalendar(trade_date=d2))
    db.add_all(
        [
            LimitPool(trade_date=d2, code="A", pool_type="up", consecutive=1),
            LimitPool(trade_date=PREV, code="A", pool_type="up", consecutive=2),
            LimitPool(trade_date=PREV, code="B", pool_type="up", consecutive=1),
            LimitPool(trade_date=DAY, code="A", pool_type="up", consecutive=3),
            LimitPool(trade_date=DAY, code="B", pool_type="up", consecutive=2),
            StockDaily(trade_date=DAY, code="A", close=10.0),
            StockDaily(trade_date=DAY, code="B", close=11.0),
            CollectLog(trade_date=PREV, task="pool_up", status="ok", rows=2),
            CollectLog(trade_date=DAY, task="pool_up", status="ok", rows=2),
            CollectLog(trade_date=DAY, task="pool_broken", status="ok", rows=0),
        ]
    )
    db.commit()
    legacy = promotion(days=3, session=db)
    index = legacy.dates.index(DAY)
    funnel = promotion_funnel(trade_date=DAY, levels="1,2", include_stocks=False, session=db)
    for level in funnel.levels:
        old = next(item for item in legacy.levels if item.level == level.level)
        assert level.base == old.counts[index]
        assert level.promoted == old.promoted[index]
        assert level.legacy_rate == old.rates[index]
    # 整体旧口径 = 各档之和
    assert legacy.overall_counts[index] == sum(item.base for item in funnel.levels)
    assert legacy.overall_promoted[index] == sum(item.promoted for item in funnel.levels)


def test_funnel_does_not_write(db):
    before = (
        db.query(LimitPool).count(),
        db.query(StockDaily).count(),
        db.query(CollectLog).count(),
    )
    db.add_all(
        [
            LimitPool(trade_date=PREV, code="A", pool_type="up", consecutive=1),
            LimitPool(trade_date=DAY, code="A", pool_type="up", consecutive=2),
            StockDaily(trade_date=DAY, code="A", close=10.0),
            CollectLog(trade_date=PREV, task="pool_up", status="ok", rows=1),
            CollectLog(trade_date=DAY, task="pool_up", status="ok", rows=1),
            CollectLog(trade_date=DAY, task="pool_broken", status="ok", rows=0),
        ]
    )
    db.commit()
    before = (
        db.query(LimitPool).count(),
        db.query(StockDaily).count(),
        db.query(CollectLog).count(),
    )
    promotion_funnel(trade_date=DAY, levels="1", include_stocks=True, session=db)
    after = (
        db.query(LimitPool).count(),
        db.query(StockDaily).count(),
        db.query(CollectLog).count(),
    )
    assert before == after
