"""竞价采样时间边界、源报价校验与帧级质量日志（P0-A-01 / P0-A-02）。"""

import contextlib
import json
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.jobs import collect_auction as ca
from app.models import AuctionSnapshot, Base, CollectLog
from app.timeutil import CST

DAY = date(2026, 11, 11)


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, 11, hour, minute, second, tzinfo=CST)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'auction.db'}", future=True)
    Base.metadata.create_all(engine)

    @contextlib.contextmanager
    def fake_scope():
        with Session(engine) as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    monkeypatch.setattr(ca, "session_scope", fake_scope)
    monkeypatch.setattr(ca, "_universe", lambda: ["600000", "600001"])
    monkeypatch.setattr(ca, "is_trade_day", lambda day: True)
    return engine


def quote(
    code: str,
    price: float,
    *,
    quote_time: str | None,
    amount: float = 10_000_000.0,
    pre_close: float = 10.0,
) -> dict:
    return {
        "code": code,
        "name": f"票{code}",
        "price": price,
        "pre_close": pre_close,
        "open": price,
        "high": price,
        "low": price,
        "volume": 1_000_000.0,
        "amount": amount,
        "turnover": 1.0,
        "volume_ratio": 1.5,
        "float_mv": 1e10,
        "total_mv": 2e10,
        "limit_up": 11.0,
        "limit_down": 9.0,
        "avg_price": price,
        "quote_time": quote_time,
        "bid1_price": price,
        "bid1_volume": 1000.0,
        "ask1_price": price,
        "ask1_volume": 1000.0,
        "committee_diff": 0.0,
    }


def snapshots(engine) -> list[AuctionSnapshot]:
    with Session(engine) as session:
        return list(session.scalars(select(AuctionSnapshot)))


def frame_logs(engine) -> list[tuple[CollectLog, dict]]:
    with Session(engine) as session:
        rows = list(
            session.scalars(
                select(CollectLog)
                .where(CollectLog.task == ca.FRAME_TASK)
                .order_by(CollectLog.id)
            )
        )
        return [(row, json.loads(row.message or "{}")) for row in rows]


@pytest.mark.parametrize(
    ("moment", "expected", "late"),
    [
        (at(9, 14, 59), None, False),
        (at(9, 15, 0), "0915", False),
        (at(9, 25, 59), "0925", False),
        (at(9, 26, 0), "0926", False),
        (at(9, 26, 59), "0926", False),
        (at(9, 27, 0), "0926", True),
        (at(9, 27, 30), "0926", True),
        (at(9, 27, 31), None, False),
        (at(9, 28, 0), None, False),
        (at(9, 30, 0), None, False),
        (at(9, 31, 0), "0931", False),
        (at(9, 31, 59), "0931", False),
        (at(9, 32, 0), "0931", True),
        (at(9, 32, 30), "0931", True),
        (at(9, 32, 31), None, False),
        (at(10, 0, 0), None, False),
        (at(11, 30, 0), None, False),
        (at(15, 0, 0), None, False),
        (at(23, 0, 0), None, False),
    ],
)
def test_frame_plan_boundaries(moment, expected, late):
    plan = ca._frame_plan(moment)
    assert (plan.frame if plan else None) == expected
    assert (plan.late if plan else False) is late


def test_frame_plan_converts_other_timezone_to_shanghai():
    # UTC 01:26 = 北京 09:26
    moment = datetime(2026, 11, 11, 1, 26, 5, tzinfo=timezone.utc)
    plan = ca._frame_plan(moment)
    assert plan is not None and plan.frame == "0926" and not plan.late


def test_non_trade_day_logs_skipped_without_writing(store, monkeypatch):
    monkeypatch.setattr(ca, "is_trade_day", lambda day: False)
    monkeypatch.setattr(ca, "fetch_spot", lambda *a, **k: pytest.fail("不应联网"))
    assert ca.collect_auction(now=at(9, 26, 5)) == 0
    assert snapshots(store) == []
    [(row, payload)] = frame_logs(store)
    assert row.status == "skipped"
    assert payload["reason"] == "NON_TRADE_DAY"
    assert payload["scheduled_frame"] == "0926"


def test_0926_writes_quote_and_ok_log_with_source_time(store, monkeypatch):
    monkeypatch.setattr(
        ca,
        "fetch_spot",
        lambda *a, **k: [
            quote("600000", 10.5, quote_time="20261111092605"),
            quote("600001", 10.2, quote_time="20261111092606"),
        ],
    )
    assert ca.collect_auction(now=at(9, 26, 10)) == 2
    rows = snapshots(store)
    assert {row.code for row in rows} == {"600000", "600001"}
    assert all(row.price in (10.5, 10.2) for row in rows)
    assert rows[0].samples[-1][0] == "0926"
    [(log, payload)] = frame_logs(store)
    assert log.status == "ok"
    assert payload["scheduled_frame"] == "0926"
    assert payload["source_quote_time"] == "20261111092606"
    assert payload["written_codes"] == 2
    assert payload["failed_batches"] == 0


def test_0927_late_unproven_skips_whole_frame(store, monkeypatch):
    monkeypatch.setattr(
        ca,
        "fetch_spot",
        lambda *a, **k: [
            quote("600000", 10.5, quote_time="20261111092705"),
        ],
    )
    assert ca.collect_auction(now=at(9, 27, 10)) == 0
    assert snapshots(store) == []
    [(log, payload)] = frame_logs(store)
    assert log.status == "skipped"
    assert payload["reason"] == "LATE_FRAME_UNPROVEN"
    assert payload["late"] is True
    assert payload["scheduled_frame"] == "0926"


def test_0927_late_proven_by_0926_quote_time_is_written(store, monkeypatch):
    monkeypatch.setattr(ca, "_universe", lambda: ["600000"])
    monkeypatch.setattr(
        ca,
        "fetch_spot",
        lambda *a, **k: [
            quote("600000", 10.5, quote_time="20261111092620"),
        ],
    )
    assert ca.collect_auction(now=at(9, 27, 10)) == 1
    assert len(snapshots(store)) == 1
    [(log, payload)] = frame_logs(store)
    assert log.status == "ok"
    assert payload["late"] is True
    assert payload["scheduled_frame"] == "0926"


def test_0926_does_not_accept_0927_quote(store, monkeypatch):
    monkeypatch.setattr(
        ca,
        "fetch_spot",
        lambda *a, **k: [
            quote("600000", 10.5, quote_time="20261111092705"),
        ],
    )
    assert ca.collect_auction(now=at(9, 26, 10)) == 0
    assert snapshots(store) == []
    [(log, payload)] = frame_logs(store)
    assert log.status == "failed"
    assert payload["dropped_newer_quote"] == 1
    assert payload["reason"] == "STALE_OR_OLD_QUOTE"


def test_0931_only_updates_existing_rows_and_keeps_auction_fields(store, monkeypatch):
    monkeypatch.setattr(
        ca,
        "fetch_spot",
        lambda *a, **k: [
            quote("600000", 10.5, quote_time="20261111092605", amount=20_000_000),
            quote("600001", 10.2, quote_time="20261111092606", amount=30_000_000),
        ],
    )
    assert ca.collect_auction(now=at(9, 26, 10)) == 2

    monkeypatch.setattr(
        ca,
        "fetch_spot",
        lambda *a, **k: [
            quote("600000", 10.9, quote_time="20261111093105", amount=99_000_000),
            quote("600001", 10.7, quote_time="20261111093106", amount=88_000_000),
            quote("600002", 9.9, quote_time="20261111093107", amount=77_000_000),
        ],
    )
    assert ca.collect_auction(now=at(9, 31, 10)) == 2
    rows = {row.code: row for row in snapshots(store)}
    assert set(rows) == {"600000", "600001"}  # 600002 没有竞价结果，不插脏行
    assert rows["600000"].price == 10.5
    assert rows["600000"].amount == 20_000_000
    assert rows["600000"].post_open_price == 10.9
    assert rows["600000"].volume_ratio == 1.5
    last_log, payload = frame_logs(store)[-1]
    assert last_log.status == "ok"
    assert payload["scheduled_frame"] == "0931"


def test_0932_late_unproven_does_not_overwrite_0931(store, monkeypatch):
    monkeypatch.setattr(
        ca,
        "fetch_spot",
        lambda *a, **k: [quote("600000", 10.5, quote_time="20261111092605")],
    )
    assert ca.collect_auction(now=at(9, 26, 10)) == 1

    monkeypatch.setattr(
        ca,
        "fetch_spot",
        lambda *a, **k: [quote("600000", 11.0, quote_time="20261111093205")],
    )
    assert ca.collect_auction(now=at(9, 32, 10)) == 0
    row = snapshots(store)[0]
    assert row.price == 10.5
    assert row.post_open_price is None
    [(log, payload)] = [item for item in frame_logs(store) if item[0].status == "skipped"]
    assert payload["scheduled_frame"] == "0931"
    assert payload["reason"] == "LATE_FRAME_UNPROVEN"


def test_same_minute_rerun_replaces_sample_instead_of_appending(store, monkeypatch):
    monkeypatch.setattr(
        ca,
        "fetch_spot",
        lambda *a, **k: [quote("600000", 10.1, quote_time="20261111092005")],
    )
    assert ca.collect_auction(now=at(9, 20, 10)) == 1
    monkeypatch.setattr(
        ca,
        "fetch_spot",
        lambda *a, **k: [quote("600000", 10.3, quote_time="20261111092050")],
    )
    assert ca.collect_auction(now=at(9, 20, 55)) == 1
    row = snapshots(store)[0]
    frames = [frame for frame in row.samples if frame[0] == "0920"]
    assert len(frames) == 1
    assert frames[0][1] == 10.3


def test_write_failure_leaves_no_rows_and_writes_failed_log(store, monkeypatch):
    monkeypatch.setattr(
        ca,
        "fetch_spot",
        lambda *a, **k: [quote("600000", 10.5, quote_time="20261111092605")],
    )

    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(ca, "upsert_many", boom)
    assert ca.collect_auction(now=at(9, 26, 10)) == 0
    assert snapshots(store) == []
    [(log, payload)] = frame_logs(store)
    assert log.status == "failed"
    assert payload["reason"] == "WRITE_EXCEPTION"


def test_outside_window_writes_nothing_at_all(store, monkeypatch):
    monkeypatch.setattr(ca, "fetch_spot", lambda *a, **k: pytest.fail("不应联网"))
    assert ca.collect_auction(now=at(10, 0, 0)) == 0
    assert snapshots(store) == []
    assert frame_logs(store) == []


def test_old_date_quote_is_dropped(store, monkeypatch):
    monkeypatch.setattr(
        ca,
        "fetch_spot",
        lambda *a, **k: [quote("600000", 10.5, quote_time="20261110092605")],
    )
    assert ca.collect_auction(now=at(9, 26, 10)) == 0
    assert snapshots(store) == []
    [(log, payload)] = frame_logs(store)
    assert log.status == "failed"
    assert payload["dropped_old_quote"] == 1


def test_partial_batch_failure_is_logged_as_partial(store, monkeypatch):
    def fake_fetch(codes, *, stats=None):
        stats.update({"requests": 2, "failed": 1})
        return [quote("600000", 10.5, quote_time="20261111092605")]

    monkeypatch.setattr(ca, "fetch_spot", fake_fetch)
    assert ca.collect_auction(now=at(9, 26, 10)) == 1
    [(log, payload)] = frame_logs(store)
    assert log.status == "partial"
    assert payload["failed_batches"] == 1
    assert payload["reason"] == "BATCH_FAILED"
    assert payload["written_codes"] == 1


def test_source_empty_is_failed_not_zero_success(store, monkeypatch):
    monkeypatch.setattr(ca, "fetch_spot", lambda *a, **k: [])
    assert ca.collect_auction(now=at(9, 26, 10)) == 0
    [(log, payload)] = frame_logs(store)
    assert log.status == "failed"
    assert payload["reason"] == "SOURCE_EMPTY"


def test_fetch_exception_is_failed(store, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(ca, "fetch_spot", boom)
    assert ca.collect_auction(now=at(9, 26, 10)) == 0
    [(log, payload)] = frame_logs(store)
    assert log.status == "failed"
    assert payload["reason"] == "FETCH_EXCEPTION"


def test_log_write_failure_rolls_back_quotes(store, monkeypatch):
    monkeypatch.setattr(
        ca,
        "fetch_spot",
        lambda *a, **k: [quote("600000", 10.5, quote_time="20261111092605")],
    )

    def boom(**kwargs):
        raise RuntimeError("log insert failed")

    monkeypatch.setattr(ca, "_log_row", boom)
    assert ca.collect_auction(now=at(9, 26, 10)) == 0
    assert snapshots(store) == []


def test_auction_frame_not_part_of_close_chain():
    from app.jobs.collect_daily import RUN_ALWAYS_TASKS, RUN_STEP_TASKS

    assert ca.FRAME_TASK not in RUN_STEP_TASKS
    assert ca.FRAME_TASK not in RUN_ALWAYS_TASKS
