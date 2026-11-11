"""竞价异动分类、质量协议与只读 API（P0-A-03 / 04 / 05 / 06）。"""

import json
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api import auction as auction_api
from app.models import (
    SAMPLE_AMOUNT,
    SAMPLE_PRICE,
    AuctionSnapshot,
    Base,
    CollectLog,
    LimitPool,
    StockBasic,
    StockConcept,
    StockDaily,
    TradeCalendar,
)
from app.services import auction_anomaly as aa

PREV_PREV = date(2026, 9, 23)
PREV = date(2026, 9, 24)
DAY = date(2026, 9, 25)


def frame(hhmm: str, price: float | None, amount: float | None = None) -> list:
    row = [None] * 8
    row[0] = hhmm
    row[SAMPLE_PRICE] = price
    row[SAMPLE_AMOUNT] = amount
    return row


def snapshot(
    code: str,
    *,
    price: float | None = 11.0,
    pre_close: float | None = 10.0,
    post_open_price: float | None = 11.5,
    samples: list | None = None,
    name: str | None = None,
) -> AuctionSnapshot:
    return AuctionSnapshot(
        trade_date=DAY,
        code=code,
        name=name or f"票{code}",
        price=price,
        pre_close=pre_close,
        pct_chg=10.0 if price and pre_close else None,
        amount=20_000_000.0,
        volume=1_000_000.0,
        post_open_price=post_open_price,
        samples=samples
        if samples is not None
        else [frame("0920", 10.8, 0.0), frame("0926", price, 20_000_000.0)],
    )


def frame_log(
    trade_date: date,
    scheduled: str,
    *,
    status: str = "ok",
    source_quote_time: str | None = "20260925092605",
    **extra,
) -> CollectLog:
    payload = {
        "trade_date": trade_date.isoformat(),
        "scheduled_frame": scheduled,
        "frame_kind": "post_open" if scheduled == "0931" else "result",
        "late": False,
        "actual_started_at": f"{trade_date.isoformat()}T09:26:05+08:00",
        "source_quote_time": source_quote_time,
        "requested_codes": 4,
        "returned_codes": 4,
        "valid_codes": 4,
        "dropped_old_quote": 0,
        "dropped_stale_quote": 0,
        "dropped_newer_quote": 0,
        "dropped_unknown_quote": 0,
        "source_time_unknown": 0,
        "failed_batches": 0,
        "total_batches": 1,
        "written_codes": 4,
        "status": status,
        "reason": None,
        "elapsed_ms": 120,
    }
    payload.update(extra)
    return CollectLog(
        trade_date=trade_date,
        task=aa.FRAME_TASK,
        status=status,
        rows=payload["written_codes"],
        message=json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            [
                TradeCalendar(trade_date=PREV_PREV),
                TradeCalendar(trade_date=PREV),
                TradeCalendar(trade_date=DAY),
            ]
        )
        # 昨日涨停 / 炸板 / 断板（C 在 PREV_PREV 涨停、PREV 未晋级 → 断板）
        db.add_all(
            [
                LimitPool(trade_date=PREV_PREV, code="C", pool_type="up", consecutive=1),
                LimitPool(trade_date=PREV, code="A", pool_type="up", consecutive=2),
                LimitPool(trade_date=PREV, code="B", pool_type="broken", consecutive=1),
                CollectLog(trade_date=PREV_PREV, task="pool_up", status="ok", rows=1),
                CollectLog(trade_date=PREV, task="pool_up", status="ok", rows=1),
                CollectLog(trade_date=PREV, task="pool_broken", status="ok", rows=1),
                StockDaily(trade_date=PREV, code="A", close=10.0, amount=100_000_000.0),
                StockDaily(trade_date=PREV, code="B", close=10.0, amount=200_000_000.0),
                StockDaily(trade_date=PREV, code="C", close=10.0, amount=50_000_000.0),
                StockBasic(code="A", name="甲", industry="电子-半导体"),
                StockBasic(code="B", name="乙", industry="机械-通用设备"),
                StockBasic(code="C", name="丙", industry="计算机-软件"),
                StockConcept(trade_date=PREV, code="A", concept="芯片"),
                StockConcept(trade_date=PREV, code="C", concept="机器人"),
            ]
        )
        db.add_all(
            [
                snapshot("A", price=11.0, post_open_price=11.5),
                snapshot("B", price=9.0, post_open_price=9.2),
                snapshot("C", price=10.5, post_open_price=None),
                snapshot("D", price=12.0, post_open_price=12.3),
                frame_log(DAY, "0926"),
                frame_log(DAY, "0931"),
            ]
        )
        db.commit()
        yield db


def build(session, **kwargs):
    params = {
        "trade_date": DAY,
        "group": aa.GROUP_ALL,
        "sort": "auction_amount",
        "order": "desc",
        "limit": 300,
    }
    params.update(kwargs)
    return aa.build_anomaly(session, **params)


def test_day_quality_valid_and_coverage(session):
    data = build(session)
    quality = data["quality"]
    assert quality["status"] == aa.VALID
    assert quality["reasons"] == []
    assert quality["auction_count"] == 4
    assert quality["result_count"] == 4
    assert quality["post_open_count"] == 3
    assert [item["scheduled_frame"] for item in quality["frames"]] == ["0926", "0931"]


def test_prev_limit_group_and_amount_ratio(session):
    data = build(session, group=aa.GROUP_PREV_LIMIT)
    assert [row["code"] for row in data["rows"]] == ["A"]
    row = data["rows"][0]
    assert row["prev_limit_status"] == "limit_up"
    assert row["prev_board_height"] == 2
    assert row["concept"] == "芯片"
    assert row["concept_source"] == "开盘啦"
    assert row["auction_price"] == 11.0
    assert row["auction_pct"] == 10.0
    assert row["auction_amount"] == 20_000_000.0
    assert row["yesterday_amount"] == 100_000_000.0
    assert row["auction_amount_ratio"] == 20.0
    assert row["pct_change_after_auction"] == 5.0
    assert row["data_quality_status"] == aa.STOCK_VALID


def test_prev_broken_group(session):
    data = build(session, group=aa.GROUP_PREV_BROKEN)
    assert [row["code"] for row in data["rows"]] == ["B"]
    row = data["rows"][0]
    assert row["prev_limit_status"] == "broken"
    assert row["prev_board_height"] == 1
    assert row["auction_pct"] == -10.0
    assert row["pct_change_after_auction"] == 2.0


def test_prev_failed_relay_group_uses_credible_failed_rows(session):
    data = build(session, group=aa.GROUP_PREV_FAILED_RELAY)
    assert [row["code"] for row in data["rows"]] == ["C"]
    row = data["rows"][0]
    assert row["prev_limit_status"] == "failed_relay"
    assert row["prev_board_height"] == 1
    assert row["post_open_price"] is None
    assert row["post_open_pct"] is None
    assert row["pct_change_after_auction"] is None
    assert "POST_OPEN_MISSING" in row["quality_reasons"]


def test_missing_0926_is_null_not_negative_100(session):
    session.add(
        snapshot(
            "E",
            price=None,
            post_open_price=10.2,
            samples=[frame("0915", 10.0), frame("0920", 10.1)],
        )
    )
    session.commit()
    data = build(session, group=aa.GROUP_ALL)
    row = next(item for item in data["rows"] if item["code"] == "E")
    assert row["auction_price"] is None
    assert row["auction_pct"] is None
    assert row["auction_amount"] is None
    assert row["auction_frame"] is None
    assert row["data_quality_status"] == aa.FRAME_MISSING


def test_missing_0931_makes_post_open_null(session):
    data = build(session, group=aa.GROUP_PREV_LIMIT)
    # C 没有 post_open_price，但它在断板组；换个视角：把 A 的开盘后价格清掉
    session.execute(
        AuctionSnapshot.__table__.update()
        .where(AuctionSnapshot.code == "A")
        .values(post_open_price=None)
    )
    session.commit()
    data = build(session, group=aa.GROUP_PREV_LIMIT)
    row = data["rows"][0]
    assert row["post_open_price"] is None
    assert row["post_open_pct"] is None
    assert row["pct_change_after_auction"] is None
    assert row["auction_pct"] == 10.0


def test_missing_yesterday_amount_keeps_ratio_null(session):
    session.execute(
        StockDaily.__table__.update()
        .where(StockDaily.trade_date == PREV, StockDaily.code == "A")
        .values(amount=0.0)
    )
    session.commit()
    data = build(session, group=aa.GROUP_PREV_LIMIT)
    row = data["rows"][0]
    assert row["yesterday_amount"] == 0.0
    assert row["auction_amount_ratio"] is None
    assert "YESTERDAY_AMOUNT_MISSING" in row["quality_reasons"]


def test_day_quality_incomplete_when_0931_missing(session):
    session.execute(
        AuctionSnapshot.__table__.update().values(post_open_price=None)
    )
    session.execute(
        CollectLog.__table__.delete().where(
            CollectLog.task == aa.FRAME_TASK, CollectLog.trade_date == DAY
        )
    )
    session.add(frame_log(DAY, "0926"))
    session.commit()
    data = build(session)
    assert data["quality"]["status"] == aa.INCOMPLETE
    assert "POST_OPEN_MISSING" in data["quality"]["reasons"]
    assert "FRAME_LOG_MISSING_0931" in data["quality"]["reasons"]


def test_day_quality_incomplete_when_source_time_unknown(session):
    session.execute(
        CollectLog.__table__.delete().where(
            CollectLog.task == aa.FRAME_TASK, CollectLog.trade_date == DAY
        )
    )
    session.add_all(
        [
            frame_log(DAY, "0926", source_quote_time=None),
            frame_log(DAY, "0931"),
        ]
    )
    session.commit()
    data = build(session, group=aa.GROUP_PREV_LIMIT)
    assert data["quality"]["status"] == aa.INCOMPLETE
    assert "SOURCE_TIME_UNKNOWN" in data["quality"]["reasons"]
    assert data["rows"][0]["data_quality_status"] == aa.SOURCE_TIME_UNKNOWN
    assert "SOURCE_TIME_UNKNOWN" in data["rows"][0]["quality_reasons"]
    assert data["rows"][0]["pct_change_after_auction"] is None


def test_missing_0931_frame_log_blocks_post_open_change(session):
    session.execute(
        CollectLog.__table__.delete().where(
            CollectLog.task == aa.FRAME_TASK,
            CollectLog.trade_date == DAY,
            CollectLog.message.like('%"scheduled_frame": "0931"%'),
        )
    )
    session.commit()
    data = build(session, group=aa.GROUP_PREV_LIMIT)
    row = data["rows"][0]
    assert row["post_open_price"] is not None
    assert "FRAME_LOG_MISSING_0931" in row["quality_reasons"]
    assert row["pct_change_after_auction"] is None


def test_prev_failed_relay_empty_when_prev_pool_untrusted(session):
    session.execute(
        CollectLog.__table__.delete().where(
            CollectLog.trade_date == PREV, CollectLog.task == "pool_up"
        )
    )
    session.commit()
    data = build(session, group=aa.GROUP_PREV_FAILED_RELAY)
    assert data["rows"] == []
    summary = next(item for item in data["groups"] if item["group"] == aa.GROUP_PREV_FAILED_RELAY)
    assert summary["total"] == 0
    assert "PREV_RELAY_UNVERIFIED" in summary["quality_reasons"]


def test_day_quality_source_unavailable_when_all_frames_failed(session):
    session.execute(
        AuctionSnapshot.__table__.delete().where(AuctionSnapshot.trade_date == DAY)
    )
    session.execute(
        CollectLog.__table__.delete().where(
            CollectLog.task == aa.FRAME_TASK, CollectLog.trade_date == DAY
        )
    )
    session.add_all(
        [
            frame_log(DAY, "0926", status="failed", source_quote_time=None),
            frame_log(DAY, "0931", status="failed", source_quote_time=None),
        ]
    )
    session.commit()
    data = build(session)
    assert data["quality"]["status"] == aa.SOURCE_UNAVAILABLE
    assert data["quality"]["auction_count"] == 0


def test_day_quality_not_collected(session):
    session.execute(AuctionSnapshot.__table__.delete())
    session.execute(
        CollectLog.__table__.delete().where(CollectLog.task == aa.FRAME_TASK)
    )
    session.commit()
    data = build(session)
    assert data["quality"]["status"] == aa.NOT_COLLECTED
    assert data["quality"]["reasons"] == ["NO_AUCTION_SNAPSHOT"]


def test_group_summaries_expose_total_and_valid(session):
    data = build(session)
    by_group = {item["group"]: item for item in data["groups"]}
    assert by_group["all"]["total"] == 4
    assert by_group["all"]["valid"] == 4
    assert by_group["prev_limit"]["total"] == 1
    assert by_group["prev_broken"]["total"] == 1
    assert by_group["prev_failed_relay"]["total"] == 1


def test_sorting_nulls_last_and_limit(session):
    data = build(session, group=aa.GROUP_ALL, sort="post_open_change", order="asc", limit=2)
    assert data["shown"] == 2
    values = [row["pct_change_after_auction"] for row in data["rows"]]
    assert values == sorted(values)


def test_api_rejects_bad_parameters(session):
    with pytest.raises(HTTPException) as exc:
        auction_api.auction_anomaly_view(
            trade_date=DAY, group="bad", sort="auction_amount", order="desc", limit=300, session=session
        )
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        auction_api.auction_anomaly_view(
            trade_date=DAY, group="all", sort="bad", order="desc", limit=300, session=session
        )
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        auction_api.auction_anomaly_view(
            trade_date=DAY, group="all", sort="auction_amount", order="bad", limit=300, session=session
        )
    assert exc.value.status_code == 400


def test_api_rejects_future_date(session):
    with pytest.raises(HTTPException) as exc:
        auction_api.auction_anomaly_view(
            trade_date=date(2099, 1, 1),
            group="all",
            sort="auction_amount",
            order="desc",
            limit=300,
            session=session,
        )
    assert exc.value.status_code == 404


def test_api_read_only(session):
    before = (
        session.query(AuctionSnapshot).count(),
        session.query(CollectLog).count(),
        session.query(LimitPool).count(),
    )
    out = auction_api.auction_anomaly_view(
        trade_date=DAY,
        group=aa.GROUP_ALL,
        sort="auction_amount",
        order="desc",
        limit=300,
        session=session,
    )
    after = (
        session.query(AuctionSnapshot).count(),
        session.query(CollectLog).count(),
        session.query(LimitPool).count(),
    )
    assert before == after
    assert out.total == 4
