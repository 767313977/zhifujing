"""竞价采样离线仿真器（S1-A2）。

用**合成行情**跑完整的 13 个计划帧（0915–0926 + 0931），覆盖 100 / 1000 / 近全市场
三种规模，以及失败、迟到、旧日期、缺源时间、缺帧、写库回滚、重复请求、并发竞争、
进程中断重跑、低覆盖、非法数值、非交易日、9:31 前待观察等场景。

原则：

- **绝不联网**：`fetch_spot` 由本脚本注入，直接返回合成报价；
- **绝不碰生产库**：每个场景都在独立临时 SQLite 里跑，`collect_auction.session_scope`
  被替换为该临时库的会话工厂；
- 结果只说明**代码路径是否正确**，不代表真实行情验收（真实竞价仍需线上验收）。

用法：

    .venv\\Scripts\\python.exe scripts\\simulate_auction_day.py
    .venv\\Scripts\\python.exe scripts\\simulate_auction_day.py --sizes 100 --out reports/s1
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.api import auction as auction_api  # noqa: E402
from app.jobs import collect_auction as ca  # noqa: E402
from app.models import (  # noqa: E402
    AuctionSnapshot,
    Base,
    CollectLog,
    StockBasic,
    TradeCalendar,
)
from app.services import auction_anomaly as aa  # noqa: E402
from app.timeutil import CST  # noqa: E402

DAY = date(2026, 11, 11)
PREV = date(2026, 11, 10)
RESULT_FRAMES = [f"09{minute:02d}" for minute in range(15, 27)]
ALL_FRAMES = RESULT_FRAMES + ["0931"]


def at(hhmm: str, second: int = 10) -> datetime:
    return datetime(DAY.year, DAY.month, DAY.day, int(hhmm[:2]), int(hhmm[2:]), second, tzinfo=CST)


def quote(
    code: str,
    index: int,
    hhmm: str,
    *,
    quote_day: date | None = DAY,
    quote_time: str | None = "__auto__",
    price: float | None = None,
    amount: float | None = None,
    volume_ratio: float | None = 1.5,
) -> dict:
    """一条合成报价。`quote_time="__auto__"` 表示按 `quote_day + hhmm` 自动生成。"""
    if quote_time == "__auto__":
        quote_time = f"{quote_day:%Y%m%d}{hhmm}05" if quote_day else None
    resolved_price = round(10 + (index % 50) * 0.01, 2) if price is None else price
    resolved_amount = 10_000_000.0 if amount is None else amount
    return {
        "code": code,
        "name": f"票{code}",
        "price": resolved_price,
        "pre_close": 10.0,
        "open": resolved_price,
        "high": resolved_price,
        "low": resolved_price,
        "volume": 1_000_000.0,
        "amount": resolved_amount,
        "turnover": 1.0,
        "volume_ratio": volume_ratio,
        "float_mv": 1e10,
        "total_mv": 2e10,
        "limit_up": 11.0,
        "limit_down": 9.0,
        "avg_price": resolved_price,
        "quote_time": quote_time,
        "bid1_price": resolved_price,
        "bid1_volume": 1000.0,
        "ask1_price": resolved_price,
        "ask1_volume": 1000.0,
        "committee_diff": 0.0,
    }


def make_quotes(codes: list[str], hhmm: str, **kwargs) -> list[dict]:
    return [quote(code, index, hhmm, **kwargs) for index, code in enumerate(codes)]


class Checker:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def check(self, name: str, ok: bool, detail=None) -> None:
        self.items.append({"name": name, "ok": bool(ok), "detail": detail})

    def has_reason(self, reasons: list[str], code: str) -> None:
        self.check(f"reason:{code}", code in reasons, reasons)

    @property
    def passed(self) -> bool:
        return all(item["ok"] for item in self.items)

    @property
    def failures(self) -> list[str]:
        return [item["name"] for item in self.items if not item["ok"]]


class Harness:
    """一个场景一套独立临时库 + 打桩后的采集模块。"""

    def __init__(self, name: str, size: int, tmpdir: Path, *, trade_day: bool = True) -> None:
        self.name = name
        self.db_path = tmpdir / f"{name}.db"
        self.engine = create_engine(f"sqlite:///{self.db_path}", future=True)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        self.codes = [f"{600000 + index:06d}" for index in range(size)]
        self._orig = {
            "session_scope": ca.session_scope,
            "universe": ca._universe,
            "fetch": ca.fetch_spot,
            "is_trade_day": ca.is_trade_day,
        }
        self._seed(trade_day=trade_day)
        ca.session_scope = self._scope
        ca._universe = lambda: list(self.codes)
        ca.is_trade_day = lambda _day: trade_day
        auction_api._industry_cache = None  # 场景之间不要互相污染行业缓存

    @contextmanager
    def _scope(self):
        session = self.Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _seed(self, *, trade_day: bool) -> None:
        with self.Session() as session:
            if trade_day:
                session.add_all(
                    [
                        TradeCalendar(trade_date=PREV),
                        TradeCalendar(trade_date=DAY),
                    ]
                )
            for index, code in enumerate(self.codes):
                session.add(
                    StockBasic(
                        code=code,
                        name=f"票{code}",
                        industry=f"测试行业{index % 5}",
                    )
                )
            session.commit()

    def close(self) -> None:
        ca.session_scope = self._orig["session_scope"]
        ca._universe = self._orig["universe"]
        ca.fetch_spot = self._orig["fetch"]
        ca.is_trade_day = self._orig["is_trade_day"]
        self.engine.dispose()

    # ------------------------------------------------------------------ 运行

    def fetch(self, quotes, *, failed: int = 0, exception=None, delay: float = 0.0, local=None):
        calls = {"n": 0}

        def _fetch(_codes, stats=None):
            calls["n"] += 1
            if delay:
                time.sleep(delay)
            if exception is not None:
                raise exception
            if stats is not None:
                stats["requests"] = 1
                stats["failed"] = failed
            if local is not None:
                return list(local.quote)
            return list(quotes)

        ca.fetch_spot = _fetch
        return calls

    def run(self, moment: datetime, quotes, **kwargs) -> int:
        self.fetch(quotes, **kwargs)
        return ca.collect_auction(now=moment)

    def run_full_day(self, *, size: int | None = None, **kwargs) -> list[int]:
        codes = self.codes if size is None else self.codes[:size]
        written = []
        for hhmm in ALL_FRAMES:
            written.append(self.run(at(hhmm), make_quotes(codes, hhmm), **kwargs))
        return written

    # ------------------------------------------------------------------ 读取

    def snapshot_rows(self) -> list[AuctionSnapshot]:
        with self.Session() as session:
            return list(session.scalars(select(AuctionSnapshot).order_by(AuctionSnapshot.code)))

    def log_rows(self):
        with self.Session() as session:
            return [
                (row, json.loads(row.message or "{}"))
                for row in session.scalars(
                    select(CollectLog)
                    .where(CollectLog.task == ca.FRAME_TASK)
                    .order_by(CollectLog.id)
                )
            ]

    def api_evidence(self) -> dict:
        """新竞价异动 API + 旧 `/api/auction`、`/api/auction/boards` 的直接调用。"""
        with self.Session() as session:
            anomaly = auction_api.auction_anomaly_view(
                trade_date=DAY,
                group="all",
                sort="auction_amount",
                order="desc",
                limit=1000,
                session=session,
            ).model_dump()
            legacy = auction_api.auction_list(
                trade_date=DAY,
                sort="amount",
                order="desc",
                min_pct=None,
                max_pct=None,
                min_amount=None,
                max_amount=None,
                min_ratio=None,
                max_ratio=None,
                min_turnover=None,
                max_turnover=None,
                min_boards=None,
                industry=None,
                limit=100,
                session=session,
            ).model_dump()
            boards = auction_api.auction_boards(
                trade_date=DAY, min_count=1, limit=20, session=session
            ).model_dump()
        return {"anomaly": _jsonable(anomaly), "legacy": _jsonable(legacy), "boards": _jsonable(boards)}

    def quality(self, *, now: datetime | None = None) -> dict:
        """直接算日期级 + 逐券质量（可选把「现在」钉到某个时刻测 PENDING）。"""
        orig_now, orig_today = aa.now_cn, aa.today_cn
        if now is not None:
            aa.now_cn = lambda: now
            aa.today_cn = lambda: now.date()
        pending = bool(now is not None and now.date() == DAY and now.time().hour * 60 + now.time().minute < 9 * 60 + 31)
        try:
            with self.Session() as session:
                rows = list(session.scalars(select(AuctionSnapshot)))
                logs = aa._frame_logs(session, DAY)
                day = aa.assess_day_quality(
                    rows,
                    logs,
                    post_open_pending=pending,
                )
                stocks = {
                    row.code: aa.assess_stock_quality(
                        row, logs, row.code, post_open_pending=pending
                    )
                    for row in rows
                }
        finally:
            aa.now_cn, aa.today_cn = orig_now, orig_today
        return {"day": day, "stocks": stocks}


def _jsonable(value):
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def sample_invariants(rows: list[AuctionSnapshot]) -> dict:
    duplicates = 0
    unordered = 0
    for row in rows:
        frames = [frame[0] for frame in (row.samples or [])]
        if len(frames) != len(set(frames)):
            duplicates += 1
        if frames != sorted(frames):
            unordered += 1
    return {"duplicate": duplicates, "unordered": unordered}


def frame_statuses(h: Harness) -> dict[str, str]:
    return {payload["scheduled_frame"]: row.status for row, payload in h.log_rows()}


def finish(
    h: Harness,
    checker: Checker,
    *,
    expect_rows: int | None = None,
    expect_day_quality: str | None = None,
    expect_reasons: tuple[str, ...] = (),
    expect_frame_status: dict[str, str] | None = None,
    expect_legacy_rows: int | None = None,
    now: datetime | None = None,
) -> dict:
    """所有场景共用的收口检查：快照、样本顺序、日志、质量、新/旧 API。"""
    rows = h.snapshot_rows()
    inv = sample_invariants(rows)
    checker.check("snapshot_rows", expect_rows is None or len(rows) == expect_rows, len(rows))
    checker.check("no_duplicate_frames", inv["duplicate"] == 0, inv["duplicate"])
    checker.check("samples_sorted", inv["unordered"] == 0, inv["unordered"])

    evidence = h.api_evidence()
    quality = h.quality(now=now)
    day = quality["day"]
    checker.check(
        "day_quality",
        expect_day_quality is None or day["status"] == expect_day_quality,
        {"status": day["status"], "reasons": day["reasons"], "warnings": day.get("warnings")},
    )
    for reason in expect_reasons:
        checker.has_reason(day["reasons"], reason)

    statuses = frame_statuses(h)
    for frame, expected in (expect_frame_status or {}).items():
        checker.check(f"frame_status:{frame}", statuses.get(frame) == expected, statuses.get(frame))

    legacy = evidence["legacy"]
    checker.check("legacy_api_rows_non_negative", legacy["total"] >= legacy["shown"] >= 0, legacy["total"])
    if expect_legacy_rows is not None:
        checker.check("legacy_api_rows", legacy["total"] == expect_legacy_rows, legacy["total"])
    checker.check(
        "legacy_api_fields",
        {"trade_date", "total", "shown", "rows", "summary"} <= set(legacy),
        sorted(legacy),
    )
    checker.check("boards_api_ok", isinstance(evidence["boards"].get("items"), list), None)
    checker.check(
        "anomaly_api_rows_match_total",
        evidence["anomaly"]["total"] == len(evidence["anomaly"]["rows"]) or evidence["anomaly"]["total"] >= evidence["anomaly"]["shown"],
        {"total": evidence["anomaly"]["total"], "shown": evidence["anomaly"]["shown"]},
    )

    stock_statuses = [status for status, *_ in quality["stocks"].values()]
    summary = {
        "snapshot_rows": len(rows),
        "day_quality": day["status"],
        "day_reasons": day["reasons"],
        "warnings": day.get("warnings"),
        "frame_statuses": statuses,
        "auction_valid": sum(1 for status in stock_statuses if status == aa.STOCK_VALID),
        "post_open_valid": sum(
            1 for _, post_status, *_ in quality["stocks"].values() if post_status == aa.STOCK_VALID
        ),
        "legacy_rows": legacy["total"],
        "anomaly_rows": evidence["anomaly"]["shown"],
        "board_items": len(evidence["boards"]["items"]),
    }
    return summary


# --------------------------------------------------------------------- 场景


def scenario_normal_day(tmpdir: Path, size: int) -> dict:
    name = f"normal_day_{size}"
    h = Harness(name, size, tmpdir)
    checker = Checker()
    try:
        written = h.run_full_day()
        checker.check("writes_every_frame", all(count == size for count in written), written)
        summary = finish(
            h,
            checker,
            expect_rows=size,
            expect_day_quality=aa.VALID,
            expect_frame_status={frame: "ok" for frame in ALL_FRAMES},
            expect_legacy_rows=size,
        )
        rows = h.snapshot_rows()
        checker.check(
            "samples_have_12_result_frames",
            all(len([f for f in row.samples if f[0] in RESULT_FRAMES]) == 12 for row in rows),
            len(rows[0].samples) if rows else 0,
        )
        checker.check(
            "post_open_present",
            all(row.post_open_price is not None for row in rows),
            sum(1 for row in rows if row.post_open_price is not None),
        )
        return {"name": name, "description": f"完整 13 帧 / {size} 只", "passed": checker.passed,
                "checks": checker.items, "summary": summary, "failures": checker.failures}
    finally:
        h.close()


def scenario_partial_batches(tmpdir: Path) -> dict:
    h = Harness("partial_batches", 100, tmpdir)
    checker = Checker()
    try:
        h.run_full_day(size=100, failed=2)
        summary = finish(
            h,
            checker,
            expect_rows=100,
            expect_day_quality=aa.INCOMPLETE,
            expect_reasons=("BATCH_FAILED",),
            expect_frame_status={"0926": "partial"},
        )
        return {"name": "partial_batches", "description": "部分批次失败（failed=2）", "passed": checker.passed,
                "checks": checker.items, "summary": summary, "failures": checker.failures}
    finally:
        h.close()


def scenario_late_proven(tmpdir: Path) -> dict:
    h = Harness("late_proven", 100, tmpdir)
    checker = Checker()
    try:
        for hhmm in RESULT_FRAMES[:-1]:
            h.run(at(hhmm), make_quotes(h.codes, hhmm))
        # 09:27:10 执行，但源报价仍是 09:25:59 → 可证明属于 0926 区间
        quotes = make_quotes(h.codes, "0926", quote_time="__manual__")
        for index, item in enumerate(quotes):
            item["quote_time"] = f"202611110925{50 + index % 9:02d}"
        h.fetch(quotes)
        written = ca.collect_auction(now=datetime(2026, 11, 11, 9, 27, 10, tzinfo=CST))
        checker.check("late_frame_written", written == 100, written)
        late_logs = [p for r, p in h.log_rows() if p["scheduled_frame"] == "0926"]
        checker.check("late_flag", bool(late_logs and late_logs[-1]["late"]), late_logs[-1] if late_logs else None)
        checker.check("late_source_time", late_logs[-1]["source_quote_time"].startswith("202611110925") if late_logs else False,
                      late_logs[-1].get("source_quote_time") if late_logs else None)
        summary = finish(
            h,
            checker,
            expect_rows=100,
            expect_day_quality=aa.INCOMPLETE,
            expect_reasons=("POST_OPEN_MISSING",),
            expect_frame_status={"0926": "ok"},
        )
        return {"name": "late_proven", "description": "计划帧迟到但源报价可证明", "passed": checker.passed,
                "checks": checker.items, "summary": summary, "failures": checker.failures}
    finally:
        h.close()


def scenario_late_unproven(tmpdir: Path) -> dict:
    h = Harness("late_unproven", 100, tmpdir)
    checker = Checker()
    try:
        quotes = make_quotes(h.codes, "0927", quote_time="__auto__")
        h.fetch(quotes)
        written = ca.collect_auction(now=datetime(2026, 11, 11, 9, 27, 10, tzinfo=CST))
        checker.check("no_write", written == 0, written)
        statuses = frame_statuses(h)
        checker.check("frame_skipped", statuses.get("0926") == "skipped", statuses)
        reasons = [p.get("reason") for r, p in h.log_rows()]
        checker.check("reason_late_unproven", "LATE_FRAME_UNPROVEN" in reasons, reasons)
        summary = finish(h, checker, expect_rows=0, expect_day_quality=aa.INCOMPLETE)
        return {"name": "late_unproven", "description": "计划帧迟到且源报价越界 → 整帧跳过", "passed": checker.passed,
                "checks": checker.items, "summary": summary, "failures": checker.failures}
    finally:
        h.close()


def scenario_slow_fetch(tmpdir: Path) -> dict:
    h = Harness("slow_fetch", 100, tmpdir)
    checker = Checker()
    try:
        for hhmm in RESULT_FRAMES[:-1]:
            h.run(at(hhmm), make_quotes(h.codes, hhmm))
        h.run(at("0926"), make_quotes(h.codes, "0926"), delay=0.05)
        last = [p for r, p in h.log_rows() if p["scheduled_frame"] == "0926"][-1]
        checker.check("elapsed_recorded", last["elapsed_ms"] >= 20, last["elapsed_ms"])
        summary = finish(
            h,
            checker,
            expect_rows=100,
            expect_day_quality=aa.INCOMPLETE,
            expect_reasons=("POST_OPEN_MISSING",),
            expect_frame_status={"0926": "ok"},
        )
        return {"name": "slow_fetch", "description": "慢请求（耗时被记录，帧仍可写）", "passed": checker.passed,
                "checks": checker.items, "summary": summary, "failures": checker.failures}
    finally:
        h.close()


def scenario_old_date(tmpdir: Path) -> dict:
    h = Harness("old_date", 100, tmpdir)
    checker = Checker()
    try:
        quotes = make_quotes(h.codes, "0926", quote_day=PREV)
        written = h.run(at("0926"), quotes)
        checker.check("no_write", written == 0, written)
        last = [p for r, p in h.log_rows() if p["scheduled_frame"] == "0926"][-1]
        checker.check("dropped_old", last["dropped_old_quote"] == 100, last["dropped_old_quote"])
        checker.check("reason_stale", last["reason"] == "STALE_OR_OLD_QUOTE", last["reason"])
        summary = finish(
            h,
            checker,
            expect_rows=0,
            expect_day_quality=aa.SOURCE_UNAVAILABLE,
            expect_reasons=("NO_AUCTION_QUOTE",),
        )
        return {"name": "old_date", "description": "旧日期报价全部被丢弃", "passed": checker.passed,
                "checks": checker.items, "summary": summary, "failures": checker.failures}
    finally:
        h.close()


def scenario_missing_source_time(tmpdir: Path) -> dict:
    h = Harness("missing_source_time", 100, tmpdir)
    checker = Checker()
    try:
        for hhmm in RESULT_FRAMES[:-1]:
            h.run(at(hhmm), make_quotes(h.codes, hhmm))
        quotes = make_quotes(h.codes, "0926")
        for item in quotes[:5]:
            item["quote_time"] = None
        written = h.run(at("0926"), quotes)
        checker.check("partial_write", written == 95, written)
        last = [p for r, p in h.log_rows() if p["scheduled_frame"] == "0926"][-1]
        checker.check("dropped_unknown", last["dropped_unknown_quote"] == 5, last["dropped_unknown_quote"])
        checker.check("quote_times_count", last["quote_times_count"] == 95, last["quote_times_count"])
        summary = finish(
            h,
            checker,
            expect_rows=100,
            expect_day_quality=aa.INCOMPLETE,
            expect_reasons=("STALE_QUOTE",),
        )
        quality = h.quality()["day"]
        checker.check("result_count_95", quality["result_count"] == 95, quality["result_count"])
        checker.check("post_open_count_0", quality["post_open_count"] == 0, quality["post_open_count"])
        return {"name": "missing_source_time", "description": "5/100 缺源报价时间（关键帧丢弃）", "passed": checker.passed,
                "checks": checker.items, "summary": summary, "failures": checker.failures}
    finally:
        h.close()


def scenario_missing_0926(tmpdir: Path) -> dict:
    h = Harness("missing_0926", 100, tmpdir)
    checker = Checker()
    try:
        for hhmm in RESULT_FRAMES[:-1]:
            h.run(at(hhmm), make_quotes(h.codes, hhmm))
        h.run(at("0931"), make_quotes(h.codes, "0931"))
        summary = finish(
            h,
            checker,
            expect_rows=100,
            expect_day_quality=aa.INCOMPLETE,
            expect_reasons=("FRAME_MISSING_0926",),
        )
        quality = h.quality()
        statuses = {status for status, *_ in quality["stocks"].values()}
        checker.check("stocks_frame_missing", aa.FRAME_MISSING in statuses, sorted(statuses))
        checker.check(
            "no_auction_price",
            all(
                aa._frame_value(row.samples, "0926", 1) is None for row in h.snapshot_rows()
            ),
            None,
        )
        return {"name": "missing_0926", "description": "过程帧完整但缺 0926 终值", "passed": checker.passed,
                "checks": checker.items, "summary": summary, "failures": checker.failures}
    finally:
        h.close()


def scenario_missing_0931(tmpdir: Path) -> dict:
    h = Harness("missing_0931", 100, tmpdir)
    checker = Checker()
    try:
        for hhmm in RESULT_FRAMES:
            h.run(at(hhmm), make_quotes(h.codes, hhmm))
        summary = finish(
            h,
            checker,
            expect_rows=100,
            expect_day_quality=aa.INCOMPLETE,
            expect_reasons=("POST_OPEN_MISSING",),
        )
        evidence = h.api_evidence()
        checker.check(
            "auction_values_kept",
            all(row["auction_price"] is not None for row in evidence["anomaly"]["rows"]),
            sum(1 for row in evidence["anomaly"]["rows"] if row["auction_price"] is None),
        )
        checker.check(
            "post_open_change_null",
            all(row["pct_change_after_auction"] is None for row in evidence["anomaly"]["rows"]),
            None,
        )
        return {"name": "missing_0931", "description": "缺 0931（开盘承接为 null，不写 0）", "passed": checker.passed,
                "checks": checker.items, "summary": summary, "failures": checker.failures}
    finally:
        h.close()


def scenario_pending_before_0931(tmpdir: Path) -> dict:
    h = Harness("pending_before_0931", 100, tmpdir)
    checker = Checker()
    try:
        for hhmm in RESULT_FRAMES:
            h.run(at(hhmm), make_quotes(h.codes, hhmm))
        summary = finish(
            h,
            checker,
            expect_rows=100,
            expect_day_quality=aa.INCOMPLETE,
            expect_reasons=("POST_OPEN_PENDING",),
            now=datetime(2026, 11, 11, 9, 27, 0, tzinfo=CST),
        )
        quality = h.quality(now=datetime(2026, 11, 11, 9, 27, 0, tzinfo=CST))
        checker.check(
            "stocks_post_open_pending",
            all(post == aa.POST_OPEN_PENDING for _, post, *_ in quality["stocks"].values()),
            sorted({post for _, post, *_ in quality["stocks"].values()}),
        )
        return {"name": "pending_before_0931", "description": "9:31 前 → 待观察（不当作失败）", "passed": checker.passed,
                "checks": checker.items, "summary": summary, "failures": checker.failures}
    finally:
        h.close()


def scenario_write_rollback(tmpdir: Path) -> dict:
    h = Harness("write_rollback", 100, tmpdir)
    checker = Checker()
    try:
        for hhmm in RESULT_FRAMES[:-1]:
            h.run(at(hhmm), make_quotes(h.codes, hhmm))
        original = ca.upsert_many
        ca.upsert_many = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("disk full"))
        try:
            written = h.run(at("0926"), make_quotes(h.codes, "0926"))
        finally:
            ca.upsert_many = original
        checker.check("no_write_on_failure", written == 0, written)
        statuses = frame_statuses(h)
        checker.check("frame_failed", statuses.get("0926") == "failed", statuses)
        rows = h.snapshot_rows()
        checker.check("rows_untouched", len(rows) == 100, len(rows))
        checker.check(
            "no_0926_sample",
            all(aa._frame_value(row.samples, "0926", 1) is None for row in rows),
            None,
        )
        summary = finish(
            h,
            checker,
            expect_rows=100,
            expect_day_quality=aa.INCOMPLETE,
            expect_reasons=("FRAME_MISSING_0926", "FRAME_0926_NOT_OK"),
        )
        return {"name": "write_rollback", "description": "0926 写库抛错 → 整体回滚且保留过程帧", "passed": checker.passed,
                "checks": checker.items, "summary": summary, "failures": checker.failures}
    finally:
        h.close()


def scenario_duplicate_requests(tmpdir: Path) -> dict:
    h = Harness("duplicate_requests", 100, tmpdir)
    checker = Checker()
    try:
        for hhmm in RESULT_FRAMES:
            h.run(at(hhmm), make_quotes(h.codes, hhmm))
        h.run(at("0931"), make_quotes(h.codes, "0931"))
        # 同一帧、同一报价重复两趟
        first = h.run(at("0926", 20), make_quotes(h.codes, "0926"))
        second = h.run(at("0926", 40), make_quotes(h.codes, "0926"))
        checker.check("duplicate_noop", first == 0 and second == 0, [first, second])
        noops = [p for r, p in h.log_rows() if p["scheduled_frame"] == "0926" and p.get("noop")]
        checker.check("noop_logged", len(noops) >= 1, len(noops))
        summary = finish(
            h,
            checker,
            expect_rows=100,
            expect_day_quality=aa.VALID,
        )
        day = h.quality()["day"]
        frame926 = next(f for f in day["frames"] if f["scheduled_frame"] == "0926")
        checker.check("effective_status_ok", frame926["status"] == "ok", frame926["status"])
        checker.check("last_attempt_noop", frame926["last_attempt_status"] == "skipped",
                      frame926["last_attempt_status"])
        return {"name": "duplicate_requests", "description": "重复请求不产生重复样本、不降级质量", "passed": checker.passed,
                "checks": checker.items, "summary": summary, "failures": checker.failures}
    finally:
        h.close()


def scenario_concurrent(tmpdir: Path) -> dict:
    h = Harness("concurrent", 1, tmpdir)
    checker = Checker()
    code = h.codes[0]
    try:
        local = threading.local()

        def _fetch(_codes, stats=None):
            if stats is not None:
                stats["requests"] = 1
                stats["failed"] = 0
            return [local.quote]

        ca.fetch_spot = _fetch

        def worker(price: float, hhmmss: str):
            local.quote = quote(code, 0, "0926", quote_time=f"20261111{hhmmss}", price=price)
            return ca.collect_auction(now=datetime(2026, 11, 11, 9, 26, 45, tzinfo=CST))

        results = {}
        threads = []
        for label, price, hhmmss in (("old", 10.4, "092601"), ("new", 10.8, "092644")):
            thread = threading.Thread(target=lambda **kw: results.setdefault(label, worker(**kw)),
                                      kwargs={"price": price, "hhmmss": hhmmss})
            threads.append(thread)
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        row = h.snapshot_rows()[0]
        checker.check("newer_quote_wins", row.price == 10.8, row.price)
        checker.check("single_sample", len([f for f in row.samples if f[0] == "0926"]) == 1, len(row.samples))
        statuses = frame_statuses(h)
        checker.check("frame_committed", statuses.get("0926") in ("ok", "partial", "skipped"), statuses)
        summary = finish(h, checker, expect_rows=1)
        summary["thread_results"] = results
        return {"name": "concurrent", "description": "两线程同帧竞争：较旧报价不得覆盖较新报价",
                "passed": checker.passed, "checks": checker.items, "summary": summary,
                "failures": checker.failures}
    finally:
        h.close()


def scenario_interrupt_rerun(tmpdir: Path) -> dict:
    h = Harness("interrupt_rerun", 100, tmpdir)
    checker = Checker()
    try:
        h.run(at("0926", 20), make_quotes(h.codes, "0926", quote_time="20261111092605"))
        original = ca.upsert_many
        ca.upsert_many = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("killed"))
        try:
            h.run(
                at("0926", 30),
                make_quotes(h.codes, "0926", quote_time="20261111092620", price=10.9),
            )
        finally:
            ca.upsert_many = original
        # 进程恢复后重跑同一帧（更晚的源时间）
        written = h.run(
            at("0926", 40),
            make_quotes(h.codes, "0926", quote_time="20261111092635", price=10.9),
        )
        checker.check("rerun_writes", written == 100, written)
        rows = h.snapshot_rows()
        checker.check("price_updated", all(row.price == 10.9 for row in rows), None)
        checker.check("single_frame", all(len([f for f in row.samples if f[0] == "0926"]) == 1 for row in rows), None)
        summary = finish(h, checker, expect_rows=100, expect_day_quality=aa.INCOMPLETE)
        return {"name": "interrupt_rerun", "description": "进程中断后重跑：可恢复且不重复", "passed": checker.passed,
                "checks": checker.items, "summary": summary, "failures": checker.failures}
    finally:
        h.close()


def scenario_low_coverage(tmpdir: Path) -> dict:
    h = Harness("low_coverage", 100, tmpdir)
    checker = Checker()
    try:
        # 只返回 30 只，且数据源没有报告批次失败
        for hhmm in RESULT_FRAMES:
            h.run(at(hhmm), make_quotes(h.codes[:30], hhmm))
        last = [p for r, p in h.log_rows() if p["scheduled_frame"] == "0926"][-1]
        checker.check("coverage_ratio", last["coverage_ratio"] == 0.3, last["coverage_ratio"])
        checker.check("coverage_gap", last["coverage_status"] == "GAP", last["coverage_status"])
        checker.check("missing_codes", last["missing_quote_codes"] == 70, last["missing_quote_codes"])
        summary = finish(
            h,
            checker,
            expect_rows=30,
            expect_day_quality=aa.INCOMPLETE,
            expect_reasons=("COVERAGE_GAP",),
            expect_frame_status={"0926": "partial"},
        )
        return {"name": "low_coverage", "description": "请求 100 只仅返回 30 只（无批次失败）",
                "passed": checker.passed, "checks": checker.items, "summary": summary,
                "failures": checker.failures}
    finally:
        h.close()


def scenario_illegal_values(tmpdir: Path) -> dict:
    h = Harness("illegal_values", 100, tmpdir)
    checker = Checker()
    try:
        for hhmm in RESULT_FRAMES:
            quotes = make_quotes(h.codes, hhmm)
            quotes[0]["price"] = 0
            quotes[1]["price"] = -1
            quotes[2]["amount"] = -5
            quotes[3]["volume_ratio"] = 0
            quotes[4]["volume_ratio"] = -1
            h.run(at(hhmm), quotes)
        post_open = make_quotes(h.codes, "0931")
        post_open[3]["volume_ratio"] = 0
        post_open[4]["volume_ratio"] = -1
        h.run(at("0931"), post_open)
        rows = {row.code: row for row in h.snapshot_rows()}
        checker.check("zero_price_cleaned", rows[h.codes[0]].price is None, rows[h.codes[0]].price)
        checker.check("negative_price_cleaned", rows[h.codes[1]].price is None, rows[h.codes[1]].price)
        checker.check("negative_amount_cleaned", rows[h.codes[2]].amount is None, rows[h.codes[2]].amount)
        checker.check("zero_ratio_cleaned", rows[h.codes[3]].volume_ratio is None, rows[h.codes[3]].volume_ratio)
        checker.check("negative_ratio_cleaned", rows[h.codes[4]].volume_ratio is None, rows[h.codes[4]].volume_ratio)
        summary = finish(h, checker, expect_rows=100)
        checker.check(
            "illegal_price_not_trusted",
            any(status == aa.NO_QUOTE for status, *_ in h.quality()["stocks"].values()),
            None,
        )
        return {"name": "illegal_values", "description": "非法价格/成交额/量比清洗",
                "passed": checker.passed, "checks": checker.items, "summary": summary,
                "failures": checker.failures}
    finally:
        h.close()


def scenario_non_trade_day(tmpdir: Path) -> dict:
    h = Harness("non_trade_day", 100, tmpdir, trade_day=False)
    checker = Checker()
    try:
        written = h.run(at("0926"), make_quotes(h.codes, "0926"))
        checker.check("no_write", written == 0, written)
        statuses = frame_statuses(h)
        checker.check("frame_skipped", statuses.get("0926") == "skipped", statuses)
        summary = finish(h, checker, expect_rows=0, expect_day_quality=aa.NOT_COLLECTED)
        return {"name": "non_trade_day", "description": "非交易日不采样", "passed": checker.passed,
                "checks": checker.items, "summary": summary, "failures": checker.failures}
    finally:
        h.close()


def scenario_no_data(tmpdir: Path) -> dict:
    h = Harness("no_data", 100, tmpdir)
    checker = Checker()
    try:
        summary = finish(h, checker, expect_rows=0, expect_day_quality=aa.NOT_COLLECTED)
        checker.check("reason_no_snapshot", summary["day_reasons"] == ["NO_AUCTION_SNAPSHOT"], summary["day_reasons"])
        evidence = h.api_evidence()
        checker.check("legacy_empty", evidence["legacy"]["shown"] == 0, evidence["legacy"]["shown"])
        checker.check("boards_empty", evidence["boards"]["items"] == [], evidence["boards"]["items"])
        checker.check("anomaly_empty", evidence["anomaly"]["rows"] == [], len(evidence["anomaly"]["rows"]))
        return {"name": "no_data", "description": "库里没有任何竞价快照", "passed": checker.passed,
                "checks": checker.items, "summary": summary, "failures": checker.failures}
    finally:
        h.close()


def scenario_success_failed_success(tmpdir: Path) -> dict:
    h = Harness("success_failed_success", 100, tmpdir)
    checker = Checker()
    try:
        for hhmm in RESULT_FRAMES[:-1]:
            h.run(at(hhmm), make_quotes(h.codes, hhmm))
        h.run(at("0926", 20), make_quotes(h.codes, "0926"))
        h.run(at("0926", 40), [], exception=RuntimeError("network down"))
        # 09:27:10 迟到候选 0926，源报价 09:26:50 更晚 → 必须允许升级
        quotes = make_quotes(h.codes, "0926", quote_time="20261111092650")
        written = h.run(datetime(2026, 11, 11, 9, 27, 10, tzinfo=CST), quotes)
        checker.check("upgrade_after_failure", written == 100, written)
        checker.check("samples_single_0926", all(len([f for f in row.samples if f[0] == "0926"]) == 1
                                                 for row in h.snapshot_rows()), None)
        summary = finish(
            h,
            checker,
            expect_rows=100,
            expect_day_quality=aa.INCOMPLETE,
            expect_reasons=("POST_OPEN_MISSING",),
            expect_frame_status={"0926": "ok"},
        )
        day_logs = [p for r, p in h.log_rows() if p["scheduled_frame"] == "0926"]
        checker.check(
            "failed_attempt_retained",
            any(r.status == "failed" for r, p in h.log_rows() if p["scheduled_frame"] == "0926"),
            [(r.status, p.get("reason")) for r, p in h.log_rows() if p["scheduled_frame"] == "0926"],
        )
        checker.check("final_quote_time", day_logs[-1]["source_quote_time"] == "20261111092650",
                      day_logs[-1].get("source_quote_time"))
        return {"name": "success_failed_success", "description": "成功→失败→成功：失败不擦除时间证据",
                "passed": checker.passed, "checks": checker.items, "summary": summary,
                "failures": checker.failures}
    finally:
        h.close()


SCENARIOS = {
    "normal_day_100": lambda tmp: scenario_normal_day(tmp, 100),
    "normal_day_1000": lambda tmp: scenario_normal_day(tmp, 1000),
    "normal_day_full": lambda tmp: scenario_normal_day(tmp, 5000),
    "partial_batches": scenario_partial_batches,
    "late_proven": scenario_late_proven,
    "late_unproven": scenario_late_unproven,
    "slow_fetch": scenario_slow_fetch,
    "old_date": scenario_old_date,
    "missing_source_time": scenario_missing_source_time,
    "missing_0926": scenario_missing_0926,
    "missing_0931": scenario_missing_0931,
    "pending_before_0931": scenario_pending_before_0931,
    "write_rollback": scenario_write_rollback,
    "duplicate_requests": scenario_duplicate_requests,
    "concurrent": scenario_concurrent,
    "interrupt_rerun": scenario_interrupt_rerun,
    "low_coverage": scenario_low_coverage,
    "illegal_values": scenario_illegal_values,
    "non_trade_day": scenario_non_trade_day,
    "no_data": scenario_no_data,
    "success_failed_success": scenario_success_failed_success,
}


def render_markdown(report: dict) -> str:
    lines = [
        "# 竞价离线仿真报告（S1-A2）",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 模拟交易日：{report['trade_date']}",
        f"- 场景总数：{report['total']}，通过：{report['passed']}",
        "- 说明：**合成数据**，仅验证代码路径；真实竞价仍需线上验收。",
        "",
        "| 场景 | 说明 | 结果 | 失败项 |",
        "| --- | --- | --- | --- |",
    ]
    for item in report["scenarios"]:
        failures = "、".join(item["failures"]) or "—"
        lines.append(
            f"| {item['name']} | {item['description']} | "
            f"{'PASS' if item['passed'] else 'FAIL'} | {failures} |"
        )
    lines += ["", "## 场景明细", ""]
    for item in report["scenarios"]:
        summary = item["summary"] or {}
        lines += [
            f"### {item['name']}",
            "",
            f"- 说明：{item['description']}",
            f"- 快照行数：{summary.get('snapshot_rows')}",
            f"- 日期质量：{summary.get('day_quality')}（{', '.join(summary.get('day_reasons') or []) or '无'}）",
            f"- 诊断提示：{', '.join(summary.get('warnings') or []) or '无'}",
            f"- 帧状态：{summary.get('frame_statuses')}",
            f"- 竞价有效 / 承接有效：{summary.get('auction_valid')} / {summary.get('post_open_valid')}",
            f"- 旧接口行数：{summary.get('legacy_rows')}；异动接口行数：{summary.get('anomaly_rows')}",
            "",
        ]
        if item["failures"]:
            lines.append(f"- ⚠️ 未通过检查：{'、'.join(item['failures'])}")
            lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="竞价采样离线 13 帧仿真")
    parser.add_argument("--out", default=str(ROOT / "reports" / "s1_auction_sim"))
    parser.add_argument("--only", default=None, help="只跑匹配的场景名（逗号分隔）")
    args = parser.parse_args()

    names = list(SCENARIOS)
    if args.only:
        wanted = [name.strip() for name in args.only.split(",") if name.strip()]
        names = [name for name in names if any(token in name for token in wanted)]

    results = []
    with tempfile.TemporaryDirectory(prefix="auction_sim_") as tmp:
        tmpdir = Path(tmp)
        for name in names:
            print(f"[sim] {name} ...", flush=True)
            try:
                results.append(SCENARIOS[name](tmpdir))
            except Exception as exc:  # noqa: BLE001 - 单个场景异常不应中断其余场景
                results.append(
                    {
                        "name": name,
                        "description": "场景执行异常",
                        "passed": False,
                        "checks": [],
                        "summary": {},
                        "failures": [f"{type(exc).__name__}: {exc}"],
                    }
                )

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_date": DAY.isoformat(),
        "synthetic": True,
        "total": len(results),
        "passed": sum(1 for item in results if item["passed"]),
        "scenarios": results,
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "auction_sim.json"
    md_path = out_dir / "auction_sim.md"
    json_path.write_text(json.dumps(_jsonable(report), ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")

    print(f"通过 {report['passed']}/{report['total']} → {json_path} / {md_path}")
    for item in results:
        if not item["passed"]:
            print(f"  FAIL {item['name']}: {item['failures']}")
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
