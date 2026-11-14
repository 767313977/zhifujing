"""R1：关键帧逐券源时间、单调覆盖与覆盖率。

这些用例与 `test_collect_auction.py` 共用 fixture/helper，但单独成文件便于对照
复审清单 A–R。
"""

from datetime import date, datetime

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
    import contextlib

    engine = create_engine(f"sqlite:///{tmp_path / 'r1.db'}", future=True)
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
    monkeypatch.setattr(ca, "is_trade_day", lambda day: True)
    return engine


def quote(code: str, price: float, quote_time: str | None) -> dict:
    return {
        "code": code,
        "name": f"票{code}",
        "price": price,
        "pre_close": 10.0,
        "open": price,
        "high": price,
        "low": price,
        "volume": 1_000_000.0,
        "amount": 10_000_000.0,
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


def snapshots(engine):
    with Session(engine) as session:
        return {row.code: row for row in session.scalars(select(AuctionSnapshot))}


def logs(engine):
    import json

    with Session(engine) as session:
        return [
            (row, json.loads(row.message or "{}"))
            for row in session.scalars(
                select(CollectLog)
                .where(CollectLog.task == ca.FRAME_TASK)
                .order_by(CollectLog.id)
            )
        ]


def run(monkeypatch, codes, quotes, moment):
    monkeypatch.setattr(ca, "_universe", lambda: list(codes))
    monkeypatch.setattr(ca, "fetch_spot", lambda *a, **k: list(quotes))
    return ca.collect_auction(now=moment)


def test_key_frame_one_known_one_unknown_source_time(store, monkeypatch):
    # 09:26 一只时间正确、一只时间缺失：缺失的那只不得写入关键帧
    assert (
        run(
            monkeypatch,
            ["600000", "600001"],
            [
                quote("600000", 10.5, "20261111092605"),
                quote("600001", 10.2, None),
            ],
            at(9, 26, 10),
        )
        == 1
    )
    rows = snapshots(store)
    assert set(rows) == {"600000"}
    last_log, payload = logs(store)[-1]
    assert last_log.status == "partial"
    assert payload["dropped_unknown_quote"] == 1
    assert payload["source_time_unknown_count"] == 0
    assert payload["quote_times"] == {"600000": "092605"}


def test_0926_source_time_unknown_writes_nothing(store, monkeypatch):
    assert (
        run(
            monkeypatch,
            ["600000"],
            [quote("600000", 10.5, None)],
            at(9, 26, 10),
        )
        == 0
    )
    assert snapshots(store) == {}
    last_log, payload = logs(store)[-1]
    assert last_log.status == "failed"
    assert payload["reason"] == "SOURCE_TIME_UNKNOWN"


def test_0931_source_time_unknown_does_not_update_post_open(store, monkeypatch):
    assert (
        run(
            monkeypatch,
            ["600000"],
            [quote("600000", 10.5, "20261111092605")],
            at(9, 26, 10),
        )
        == 1
    )
    assert (
        run(
            monkeypatch,
            ["600000"],
            [quote("600000", 11.0, None)],
            at(9, 31, 10),
        )
        == 0
    )
    row = snapshots(store)["600000"]
    assert row.price == 10.5
    assert row.post_open_price is None
    last_log, payload = logs(store)[-1]
    assert last_log.status == "failed"
    assert payload["dropped_unknown_quote"] == 1


def test_source_time_list_truncates_but_counts_full(store, monkeypatch):
    codes = [f"60{i:04d}" for i in range(205)]
    quotes = [quote(code, 10.5, None) for code in codes]
    assert run(monkeypatch, codes, quotes, at(9, 20, 10)) == 205
    last_log, payload = logs(store)[-1]
    assert last_log.status == "partial"
    assert payload["source_time_unknown_count"] == 205
    assert len(payload["source_time_unknown_codes"]) == 200
    assert payload["source_time_unknown_codes_truncated"] is True


def test_dropped_newer_time_is_not_reported_as_accepted(store, monkeypatch):
    assert (
        run(
            monkeypatch,
            ["600000", "600001"],
            [
                quote("600000", 10.5, "20261111092605"),
                quote("600001", 10.2, "20261111092700"),  # 晚于 0926，应丢弃
            ],
            at(9, 26, 10),
        )
        == 1
    )
    last_log, payload = logs(store)[-1]
    assert payload["source_quote_time"] == "20261111092605"
    assert payload["raw_source_quote_time_max"] == "20261111092700"
    assert payload["dropped_newer_quote"] == 1


def test_older_quote_cannot_overwrite_newer_0926(store, monkeypatch):
    assert (
        run(
            monkeypatch,
            ["600000"],
            [quote("600000", 10.5, "20261111092618")],
            at(9, 26, 20),
        )
        == 1
    )
    # 09:27 迟到候选 0926，报价时间 09:25:59 更旧：不得覆盖 09:26:18
    assert (
        run(
            monkeypatch,
            ["600000"],
            [quote("600000", 10.4, "20261111092559")],
            at(9, 27, 10),
        )
        == 0
    )
    row = snapshots(store)["600000"]
    assert row.price == 10.5
    last_log, payload = logs(store)[-1]
    assert last_log.status == "skipped"
    assert payload["reason"] == "NO_NEWER_QUOTE"
    assert payload["noop"] is True


def test_newer_quote_upgrades_0926_without_duplicate_sample(store, monkeypatch):
    assert (
        run(
            monkeypatch,
            ["600000"],
            [quote("600000", 10.5, "20261111092618")],
            at(9, 26, 20),
        )
        == 1
    )
    assert (
        run(
            monkeypatch,
            ["600000"],
            [quote("600000", 10.6, "20261111092640")],
            at(9, 26, 50),
        )
        == 1
    )
    row = snapshots(store)["600000"]
    assert row.price == 10.6
    frames = [frame for frame in row.samples if frame[0] == "0926"]
    assert len(frames) == 1
    last_log, _ = logs(store)[-1]
    assert last_log.status == "ok"


def test_older_0931_cannot_overwrite_newer_post_open(store, monkeypatch):
    assert (
        run(
            monkeypatch,
            ["600000"],
            [quote("600000", 10.5, "20261111092605")],
            at(9, 26, 10),
        )
        == 1
    )
    assert (
        run(
            monkeypatch,
            ["600000"],
            [quote("600000", 11.5, "20261111093120")],
            at(9, 31, 25),
        )
        == 1
    )
    assert (
        run(
            monkeypatch,
            ["600000"],
            [quote("600000", 11.4, "20261111093110")],
            at(9, 32, 0),
        )
        == 0
    )
    row = snapshots(store)["600000"]
    assert row.post_open_price == 11.5
    last_log, payload = logs(store)[-1]
    assert last_log.status == "skipped"
    assert payload["reason"] == "NO_NEWER_QUOTE"


def test_monotonic_check_uses_persisted_log_not_stale_snapshot(store, monkeypatch):
    assert (
        run(
            monkeypatch,
            ["600000"],
            [quote("600000", 10.6, "20261111092640")],
            at(9, 26, 45),
        )
        == 1
    )
    # 模拟另一事务已把日志写成 09:26:40，但快照仍是旧值 10.5
    with Session(store) as session:
        session.execute(
            AuctionSnapshot.__table__.update()
            .where(AuctionSnapshot.code == "600000")
            .values(price=10.5)
        )
        session.commit()
    # 09:26:30 比日志里的 09:26:40 旧：必须忽略，而不是看快照里的 10.5
    assert (
        run(
            monkeypatch,
            ["600000"],
            [quote("600000", 10.55, "20261111092630")],
            at(9, 27, 0),
        )
        == 0
    )
    assert snapshots(store)["600000"].price == 10.5


def test_coverage_full_100_of_100(store, monkeypatch):
    codes = [f"60{i:04d}" for i in range(100)]
    quotes = [quote(code, 10.5, "20261111092605") for code in codes]
    assert run(monkeypatch, codes, quotes, at(9, 26, 10)) == 100
    last_log, payload = logs(store)[-1]
    assert last_log.status == "ok"
    assert payload["coverage_ratio"] == 1.0
    assert payload["coverage_status"] == "FULL"
    assert payload["missing_quote_codes"] == 0


def test_coverage_95_of_100_is_not_ok(store, monkeypatch):
    codes = [f"60{i:04d}" for i in range(100)]
    quotes = [quote(code, 10.5, "20261111092605") for code in codes[:95]]
    assert run(monkeypatch, codes, quotes, at(9, 26, 10)) == 95
    last_log, payload = logs(store)[-1]
    assert last_log.status == "partial"
    assert payload["coverage_ratio"] == 0.95
    assert payload["coverage_status"] == "GAP"
    assert payload["missing_quote_codes"] == 5
    assert payload["reason"] == "COVERAGE_GAP"


def test_coverage_1_of_100_is_partial_gap(store, monkeypatch):
    codes = [f"60{i:04d}" for i in range(100)]
    quotes = [quote(codes[0], 10.5, "20261111092605")]
    assert run(monkeypatch, codes, quotes, at(9, 26, 10)) == 1
    last_log, payload = logs(store)[-1]
    assert last_log.status == "partial"
    assert payload["coverage_ratio"] == 0.01
    assert payload["coverage_status"] == "GAP"
    assert payload["missing_quote_codes"] == 99


def test_0931_only_updates_existing_candidates_and_reports_gap(store, monkeypatch):
    codes = [f"60{i:04d}" for i in range(100)]
    quotes = [quote(code, 10.5, "20261111092605") for code in codes]
    assert run(monkeypatch, codes, quotes, at(9, 26, 10)) == 100

    subset = [quote(code, 11.0, "20261111093105") for code in codes[:50]]
    assert run(monkeypatch, codes, subset, at(9, 31, 10)) == 50
    last_log, payload = logs(store)[-1]
    assert last_log.status == "partial"
    assert payload["eligible_existing_codes"] == 100
    assert payload["returned_codes"] == 50
    assert payload["missing_quote_codes"] == 50
    assert payload["reason"] == "COVERAGE_GAP"
