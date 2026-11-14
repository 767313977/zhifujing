"""S5-B：数据质量 v2 契约（竞价双维度 / 三池拆分 / SOURCE_ERROR）。

全部独立内存 SQLite，不连真实库、不发外部请求。
"""

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models import (
    AuctionSnapshot,
    Base,
    CollectLog,
    LimitPool,
    MarketSentiment,
    StockBasic,
    StockDaily,
    TradeCalendar,
)
from app.services import data_quality as dq

D1 = date(2026, 11, 3)
D2 = date(2026, 11, 4)
D3 = date(2026, 11, 5)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([TradeCalendar(trade_date=day) for day in (D1, D2, D3)])
        db.add(StockBasic(code="600000", name="合成银行", industry="银行"))
        db.add(StockDaily(trade_date=D1, code="600000", open=10, close=10, pct_chg=1))
        db.add(
            MarketSentiment(
                trade_date=D1,
                limit_up_count=30,
                broken_rate=20.0,
                max_consecutive=2,
                up_count=3000,
                down_count=2000,
            )
        )
        db.add(LimitPool(trade_date=D1, code="600000", pool_type="up", name="合成银行", consecutive=1))
        for task, rows in (("pool_up", 1), ("pool_broken", 0), ("pool_down", 0)):
            db.add(
                CollectLog(
                    trade_date=D1,
                    task=task,
                    status="ok",
                    rows=rows,
                    created_at=datetime(2026, 11, 3, 16, 0),
                )
            )
        db.commit()
        yield db


def _item(result, source):
    return {item["source"]: item for item in result["items"]}[source]


def _frame_log(day, frame, moment, *, status="ok", message=None):
    payload = {"scheduled_frame": frame}
    if message:
        payload.update(message)
    return CollectLog(
        trade_date=day,
        task="auction_frame",
        status=status,
        rows=1,
        message=str(payload).replace("'", '"'),
        created_at=moment,
    )


def _valid_frame_payload(frame):
    return {
        "requested_codes": 1,
        "returned_codes": 1,
        "valid_codes": 1,
        "written_codes": 1,
        "quote_times_count": 1,
        "quote_times": {"600000": f"09:{frame[2:]}:00"},
    }


def _auction_row(day, *, price=10.0, post_open=None):
    return AuctionSnapshot(
        trade_date=day,
        code="600000",
        price=price,
        samples=[["0926", price, 1000.0]],
        post_open_price=post_open,
    )


# ---------------------------------------------------------------- 竞价双维度


def test_auction_0926_valid_without_0931_is_partial_not_verified(session):
    session.add(_auction_row(D1, post_open=None))
    session.add(_frame_log(D1, "0926", datetime(2026, 11, 3, 9, 27), message=_valid_frame_payload("0926")))
    session.commit()

    item = _item(dq.overview(session, target_date=D1, days=10), "auction_snapshot")
    assert item["auction_result_status"] == dq.AUCTION_VALID
    assert item["post_open_status"] == dq.AUCTION_MISSING
    assert item["status"] == dq.STATUS_PARTIAL
    assert item["completion_proof"] is False
    assert "AUCTION_0931_MISSING" in item["reason_codes"]


def test_auction_partial_0926_frame_is_incomplete(session):
    session.add(_auction_row(D1, post_open=10.2))
    session.add(
        _frame_log(
            D1,
            "0926",
            datetime(2026, 11, 3, 9, 27),
            status="partial",
            message={"failed_batches": 1},
        )
    )
    session.add(_frame_log(D1, "0931", datetime(2026, 11, 3, 9, 32), message=_valid_frame_payload("0931")))
    session.commit()

    item = _item(dq.overview(session, target_date=D1, days=10), "auction_snapshot")
    assert item["auction_result_status"] == dq.AUCTION_INCOMPLETE
    assert item["status"] == dq.STATUS_PARTIAL
    assert item["completion_proof"] is False
    assert "FRAME_0926_PARTIAL" in item["reason_codes"]


def test_auction_both_dimensions_valid_is_verified_but_real_acceptance_pending(session):
    session.add(_auction_row(D1, post_open=10.2))
    session.add(_frame_log(D1, "0926", datetime(2026, 11, 3, 9, 27), message=_valid_frame_payload("0926")))
    session.add(_frame_log(D1, "0931", datetime(2026, 11, 3, 9, 32), message=_valid_frame_payload("0931")))
    session.commit()

    item = _item(dq.overview(session, target_date=D1, days=10), "auction_snapshot")
    assert item["auction_result_status"] == dq.AUCTION_VALID
    assert item["post_open_status"] == dq.AUCTION_VALID
    assert item["status"] == dq.STATUS_VERIFIED
    assert item["completion_proof"] is True
    # 质量维度完整 ≠ 真实实盘验收通过
    assert "REAL_AUCTION_DATA_NOT_YET_VERIFIED" in item["reason_codes"]


def test_auction_failed_frame_with_rows_is_incomplete_not_verified(session):
    session.add(_auction_row(D1))
    session.add(_frame_log(D1, "0926", datetime(2026, 11, 3, 9, 27), status="failed"))
    session.commit()

    item = _item(dq.overview(session, target_date=D1, days=10), "auction_snapshot")
    assert item["auction_result_status"] == dq.AUCTION_INCOMPLETE
    assert item["status"] == dq.STATUS_PARTIAL
    assert item["completion_proof"] is False
    assert "FRAME_0926_NOT_OK" in item["reason_codes"]


def test_auction_all_failed_without_rows_is_unverifiable(session):
    session.add(_frame_log(D1, "0926", datetime(2026, 11, 3, 9, 27), status="failed"))
    session.commit()

    item = _item(dq.overview(session, target_date=D1, days=10), "auction_snapshot")
    assert item["auction_result_status"] == dq.AUCTION_UNVERIFIABLE
    assert item["status"] == dq.STATUS_UNVERIFIABLE
    assert item["completion_proof"] is False


def test_auction_rows_without_frame_logs_are_not_verified(session):
    session.add(_auction_row(D1, post_open=10.2))
    session.commit()

    item = _item(dq.overview(session, target_date=D1, days=10), "auction_snapshot")
    assert item["auction_result_status"] == dq.AUCTION_INCOMPLETE
    assert item["status"] == dq.STATUS_PARTIAL
    assert item["completion_proof"] is False
    assert "FRAME_LOG_MISSING_0926" in item["reason_codes"]


def test_auction_0931_pending_before_open(session, monkeypatch):
    monkeypatch.setattr(dq, "today_cn", lambda: D1)
    monkeypatch.setattr(dq, "now_cn", lambda: datetime(2026, 11, 3, 9, 0))
    session.add(_auction_row(D1))
    session.add(_frame_log(D1, "0926", datetime(2026, 11, 3, 9, 27), message=_valid_frame_payload("0926")))
    session.commit()

    item = _item(dq.overview(session, target_date=D1, days=10), "auction_snapshot")
    assert item["post_open_status"] == dq.AUCTION_PENDING
    assert item["status"] == dq.STATUS_PARTIAL
    assert "AUCTION_0931_PENDING" in item["reason_codes"]


def test_auction_without_rows_or_logs_is_not_collected(session):
    item = _item(dq.overview(session, target_date=D2, days=10), "auction_snapshot")
    assert item["status"] == dq.STATUS_NOT_COLLECTED
    assert item["auction_result_status"] == dq.AUCTION_NOT_COLLECTED
    assert item["completion_proof"] is False


# ---------------------------------------------------------------- 三池拆分


def _add_pools(session, day, *, up=1, broken=0, down=0, up_status="ok", broken_status="ok", down_status="ok"):
    if up:
        session.add(LimitPool(trade_date=day, code="600000", pool_type="up", name="合成银行", consecutive=1))
    for task, rows, status in (
        ("pool_up", up, up_status),
        ("pool_broken", broken, broken_status),
        ("pool_down", down, down_status),
    ):
        session.add(
            CollectLog(
                trade_date=day,
                task=task,
                status=status,
                rows=rows,
                created_at=datetime(day.year, day.month, day.day, 16, 0),
            )
        )


def test_pool_up_ok_but_broken_failed_is_not_combined_verified(session):
    _add_pools(session, D2, up=1, broken_status="failed")
    session.commit()

    item = _item(dq.overview(session, target_date=D2, days=10), "limit_pool")
    assert item["status"] == dq.STATUS_VERIFIED  # up 单来源可成功
    assert item["pool_broken"]["status"] == dq.STATUS_UNVERIFIABLE
    assert item["combined_view_status"] == dq.STATUS_UNVERIFIABLE
    assert item["completion_proof"] is False
    assert item["up_completion_proof"] is True
    assert item["broken_completion_proof"] is False


def test_pool_up_ok_but_broken_log_missing_is_not_combined_verified(session):
    _add_pools(session, D2)
    session.query(CollectLog).filter(
        CollectLog.trade_date == D2, CollectLog.task == "pool_broken"
    ).delete()
    session.commit()

    item = _item(dq.overview(session, target_date=D2, days=10), "limit_pool")
    assert item["pool_broken"]["status"] == dq.STATUS_NOT_COLLECTED
    assert item["combined_view_status"] == dq.STATUS_UNVERIFIABLE
    assert item["completion_proof"] is False


def test_all_three_pools_ok_is_combined_verified(session):
    _add_pools(session, D2, up=1, broken=0, down=0)
    session.commit()

    item = _item(dq.overview(session, target_date=D2, days=10), "limit_pool")
    assert item["pool_broken"]["status"] == dq.STATUS_VERIFIED
    assert item["pool_down"]["status"] == dq.STATUS_VERIFIED
    assert item["combined_view_status"] == dq.STATUS_VERIFIED
    assert item["completion_proof"] is True


def test_pool_latest_failure_keeps_previous_snapshot_flags(session):
    _add_pools(session, D2, up=1, broken=1)
    session.add(
        CollectLog(
            trade_date=D2,
            task="pool_up",
            status="failed",
            rows=0,
            created_at=datetime(2026, 11, 4, 20, 0),
        )
    )
    session.commit()

    item = _item(dq.overview(session, target_date=D2, days=10), "limit_pool")
    assert item["status"] == dq.STATUS_PARTIAL
    assert item["previous_snapshot_available"] is True
    assert item["current_attempt_failed"] is True
    assert item["combined_view_status"] == dq.STATUS_PARTIAL
    assert item["completion_proof"] is False


# ---------------------------------------------------------------- SOURCE_ERROR


def test_schema_error_is_source_error_not_not_collected(session, monkeypatch):
    def boom(*_args, **_kwargs):
        raise OperationalError("SELECT 1", {}, Exception("no such table: stock_daily"))

    monkeypatch.setattr(dq, "_stock_daily_items", boom)
    item = _item(dq.overview(session, target_date=D1, days=10), "stock_daily")
    assert item["status"] == dq.STATUS_SOURCE_ERROR
    assert item["source"] == "stock_daily"
    assert item["reason_codes"] == ["DB_SCHEMA_UNAVAILABLE"]
    assert "无法断言未采集" in item["note"]
    # 不泄露 SQL / 路径 / 连接串
    text = str(item)
    assert "SELECT 1" not in text
    assert "stock_daily" in text  # 来源名本身是安全可公开的


def test_query_error_is_source_error_with_safe_code(session, monkeypatch):
    def boom(*_args, **_kwargs):
        raise OperationalError("SELECT 1", {}, Exception("database is locked"))

    monkeypatch.setattr(dq, "_limit_pool_items", boom)
    item = _item(dq.overview(session, target_date=D1, days=10), "limit_pool")
    assert item["status"] == dq.STATUS_SOURCE_ERROR
    assert item["reason_codes"] == ["DB_QUERY_FAILED"]
    assert "database is locked" not in str(item)


def test_one_source_error_does_not_break_others(session, monkeypatch):
    def boom(*_args, **_kwargs):
        raise OperationalError("SELECT 1", {}, Exception("no such table: auction_snapshot"))

    monkeypatch.setattr(dq, "_auction_items", boom)
    result = dq.overview(session, target_date=D1, days=10)
    by_source = {item["source"]: item for item in result["items"]}
    assert by_source["auction_snapshot"]["status"] == dq.STATUS_SOURCE_ERROR
    assert by_source["stock_daily"]["status"] == dq.STATUS_VERIFIED
    assert by_source["trade_calendar"]["status"] == dq.STATUS_VERIFIED


def test_timeline_keeps_source_error_bucket(session, monkeypatch):
    def boom(*_args, **_kwargs):
        raise OperationalError("SELECT 1", {}, Exception("no such table: sector_member"))

    monkeypatch.setattr(dq, "_sector_member_items", boom)
    monkeypatch.setattr(dq, "today_cn", lambda: D3)
    data = dq.timeline(session, source="sector_member", days=3)
    assert all(point["status"] == dq.STATUS_SOURCE_ERROR for point in data["points"])
    assert data["quality_version"] == "data-quality-v2"


# ---------------------------------------------------------------- 版本与只读


def test_quality_version_is_versioned(session):
    data = dq.overview(session, target_date=D1, days=10)
    assert data["quality_version"] == dq.QUALITY_VERSION == "data-quality-v2"
    timeline = dq.timeline(session, source="limit_pool", days=3)
    assert timeline["quality_version"] == dq.QUALITY_VERSION


def test_status_v2_mapping_is_total():
    assert set(dq.STATUS_V2_MAP) == {
        "SOURCE_VERIFIED",
        "SOURCE_PARTIAL",
        "SOURCE_UNVERIFIABLE",
        "SOURCE_NOT_COLLECTED",
        "SOURCE_ERROR",
        "SOURCE_PENDING",
    }
    assert dq.STATUS_V2_MAP["SOURCE_VERIFIED"] == dq.STATUS_VERIFIED
    assert dq.STATUS_V2_MAP["SOURCE_ERROR"] == dq.STATUS_SOURCE_ERROR


def test_quality_service_stays_read_only_under_errors(session, monkeypatch):
    before = (
        session.scalar(select(func.count()).select_from(LimitPool)),
        session.scalar(select(func.count()).select_from(CollectLog)),
        session.scalar(select(func.count()).select_from(AuctionSnapshot)),
    )

    def boom(*_args, **_kwargs):
        raise OperationalError("SELECT 1", {}, Exception("no such table: limit_pool"))

    monkeypatch.setattr(dq, "_limit_pool_items", boom)
    dq.overview(session, target_date=D1, days=10)
    after = (
        session.scalar(select(func.count()).select_from(LimitPool)),
        session.scalar(select(func.count()).select_from(CollectLog)),
        session.scalar(select(func.count()).select_from(AuctionSnapshot)),
    )
    assert before == after
