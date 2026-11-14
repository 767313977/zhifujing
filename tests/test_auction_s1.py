"""S1-A1：成功→失败→成功的时间证据链、并发写锁与重复采样单调性。

R1 的 `_previous_frame_quote_times()` 取「最新一条日志」的 `quote_times`：只要
中间夹一次失败/跳过，时间证据就被擦成空，第三次合法升级会被误判成「不更新」。
这里逐条锁住修复后的行为。
"""

import contextlib
import json
import sqlite3
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.jobs import collect_auction as ca
from app.models import AuctionSnapshot, Base, CollectLog
from app.services import auction_anomaly as aa
from app.timeutil import CST

DAY = date(2026, 11, 11)


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, 11, hour, minute, second, tzinfo=CST)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    db_path = tmp_path / "s1.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
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
    return db_path, engine


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


def snapshots(engine) -> dict[str, AuctionSnapshot]:
    with Session(engine) as session:
        return {row.code: row for row in session.scalars(select(AuctionSnapshot))}


def logs(engine):
    with Session(engine) as session:
        return [
            (row, json.loads(row.message or "{}"))
            for row in session.scalars(
                select(CollectLog)
                .where(CollectLog.task == ca.FRAME_TASK)
                .order_by(CollectLog.id)
            )
        ]


def run(monkeypatch, quotes, moment, codes=("600000",)):
    monkeypatch.setattr(ca, "_universe", lambda: list(codes))
    monkeypatch.setattr(ca, "fetch_spot", lambda *a, **k: list(quotes))
    return ca.collect_auction(now=moment)


def _frame_entries(engine, frame: str):
    return [(row, payload) for row, payload in logs(engine) if payload["scheduled_frame"] == frame]


# --------------------------------------------------------------- 成功→失败→成功


def test_success_failed_success_allows_legal_upgrade(store, monkeypatch):
    _, engine = store
    assert run(monkeypatch, [quote("600000", 10.5, "20261111092618")], at(9, 26, 20)) == 1

    # 第二次尝试失败：只写 failed 日志，不更新行情
    def boom(*_a, **_k):
        raise RuntimeError("network down")

    monkeypatch.setattr(ca, "fetch_spot", boom)
    assert run(monkeypatch, [], at(9, 26, 40)) == 0
    failed_logs = [row for row, payload in _frame_entries(engine, "0926") if row.status == "failed"]
    assert len(failed_logs) == 1
    assert failed_logs[0].message and "quote_times" in failed_logs[0].message

    # 第三次成功、源时间更晚：必须允许升级
    assert run(monkeypatch, [quote("600000", 10.6, "20261111092650")], at(9, 27, 10)) == 1
    row = snapshots(engine)["600000"]
    assert row.price == 10.6
    frames = [frame for frame in row.samples if frame[0] == "0926"]
    assert len(frames) == 1
    assert frames[0][1] == 10.6

    effective = [payload for r, payload in _frame_entries(engine, "0926") if r.status == "ok"]
    assert effective[-1]["quote_times"] == {"600000": "092650"}


def test_success_noop_success_allows_legal_upgrade(store, monkeypatch):
    _, engine = store
    assert run(monkeypatch, [quote("600000", 10.5, "20261111092618")], at(9, 26, 20)) == 1
    # 完全相同的报价重复采样 → noop
    assert run(monkeypatch, [quote("600000", 10.5, "20261111092618")], at(9, 26, 30)) == 0
    assert run(monkeypatch, [quote("600000", 10.55, "20261111092633")], at(9, 26, 35)) == 1
    assert snapshots(engine)["600000"].price == 10.55


def test_failed_attempt_after_success_does_not_invalidate_snapshot(store, monkeypatch):
    _, engine = store
    assert run(monkeypatch, [quote("600000", 10.5, "20261111092618")], at(9, 26, 20)) == 1
    assert run(monkeypatch, [quote("600000", 11.0, "20261111093105")], at(9, 31, 10)) == 1

    def boom(*_a, **_k):
        raise RuntimeError("network down")

    monkeypatch.setattr(ca, "fetch_spot", boom)
    assert run(monkeypatch, [], at(9, 26, 50)) == 0

    with Session(engine) as session:
        rows = list(session.scalars(select(AuctionSnapshot)))
        frame_logs = aa._frame_logs(session, DAY)
        quality = aa.assess_day_quality(rows, frame_logs)

    assert quality["status"] == aa.VALID
    assert "FRAME_0926_NOT_OK" not in quality["reasons"]
    assert quality["warnings"] == ["FRAME_0926_LAST_ATTEMPT_FAILED"]
    frame926 = next(f for f in quality["frames"] if f["scheduled_frame"] == "0926")
    assert frame926["status"] == "ok"
    assert frame926["last_attempt_status"] == "failed"
    assert frame926["last_attempt_failed"] is True
    assert frame926["last_effective_status"] == "ok"


def test_crash_rollback_keeps_previous_quote_and_time_evidence(store, monkeypatch):
    _, engine = store
    assert run(monkeypatch, [quote("600000", 10.5, "20261111092618")], at(9, 26, 20)) == 1

    def broken_upsert(*_a, **_k):
        raise RuntimeError("disk full")

    real_upsert = ca.upsert_many
    monkeypatch.setattr(ca, "upsert_many", broken_upsert)
    assert run(monkeypatch, [quote("600000", 10.6, "20261111092630")], at(9, 26, 35)) == 0
    assert snapshots(engine)["600000"].price == 10.5

    monkeypatch.setattr(ca, "upsert_many", real_upsert)
    # 恢复后：更晚的时间仍能合法升级（失败日志没有擦掉 092618）
    assert run(monkeypatch, [quote("600000", 10.7, "20261111092645")], at(9, 26, 50)) == 1
    assert snapshots(engine)["600000"].price == 10.7


# ------------------------------------------------------------------ 单调性顺序


def test_older_then_newer_upgrades(store, monkeypatch):
    _, engine = store
    assert run(monkeypatch, [quote("600000", 10.4, "20261111092559")], at(9, 27, 5)) == 1
    assert run(monkeypatch, [quote("600000", 10.6, "20261111092640")], at(9, 27, 20)) == 1
    assert snapshots(engine)["600000"].price == 10.6


def test_newer_then_older_is_ignored_in_both_orders(store, monkeypatch):
    _, engine = store
    assert run(monkeypatch, [quote("600000", 10.6, "20261111092640")], at(9, 26, 45)) == 1
    assert run(monkeypatch, [quote("600000", 10.4, "20261111092601")], at(9, 27, 5)) == 0
    assert snapshots(engine)["600000"].price == 10.6
    last_row, last_payload = logs(engine)[-1]
    assert last_payload["reason"] == "NO_NEWER_QUOTE"
    assert last_payload["noop"] is True


def test_partial_then_full_retry_fills_missing_codes(store, monkeypatch):
    """第一次 partial（95/100），迟到重试补齐 5 只且全部升级 → 当日可恢复 VALID。"""
    _, engine = store
    codes = [f"60{i:04d}" for i in range(100)]
    first = [quote(code, 10.5, "20261111092605") for code in codes[:95]]
    assert run(monkeypatch, first, at(9, 26, 10), codes=codes) == 95

    # 09:27:10 迟到重试：100 只全部给出更晚的 0926:50
    retry = [quote(code, 10.6, "20261111092650") for code in codes]
    monkeypatch.setattr(ca, "fetch_spot", lambda *a, **k: list(retry))
    assert ca.collect_auction(now=datetime(2026, 11, 11, 9, 27, 10, tzinfo=CST)) == 100

    day_one = [quote(code, 11.0, "20261111093105") for code in codes]
    assert run(monkeypatch, day_one, at(9, 31, 10), codes=codes) == 100

    assert len(snapshots(engine)) == 100
    with Session(engine) as session:
        rows = list(session.scalars(select(AuctionSnapshot)))
        frame_logs = aa._frame_logs(session, DAY)
        quality = aa.assess_day_quality(rows, frame_logs)
    assert quality["status"] == aa.VALID
    assert quality["result_count"] == 100



# ------------------------------------------------------------------ 并发写锁


def test_write_lock_serializes_other_writers(store):
    """`_acquire_write_lock` 真的拿到 SQLite 写锁：别的连接随即无法开写事务。"""
    db_path, engine = store
    with Session(engine) as session:
        ca._acquire_write_lock(session)
        other = sqlite3.connect(str(db_path), timeout=0.05, isolation_level=None)
        try:
            with pytest.raises(sqlite3.OperationalError):
                other.execute("BEGIN IMMEDIATE")
        finally:
            other.close()
        session.commit()


def test_two_sequential_sessions_see_committed_time_evidence(store, monkeypatch):
    """跨连接读取：第二次采样必须看到第一次提交的 quote_times。"""
    _, engine = store
    assert run(monkeypatch, [quote("600000", 10.5, "20261111092618")], at(9, 26, 20)) == 1
    with Session(engine) as session:
        times = ca._previous_frame_quote_times(session, DAY, "0926")
    assert times == {"600000": "092618"}
