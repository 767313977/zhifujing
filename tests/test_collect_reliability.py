"""审计 A 的后端修复回归用例（2026-10-22 体检）。

覆盖 9 项（编号与任务 task-5 一致）：

1. 历史日 ETF 不得回退 iFinD（`EtfNotReady` → 步骤记 skipped）；
2. 三池纳入 `has_collected` 判据，且「超期 skipped」不能算 failed；
3. `backfill_index` 问过 0 行要抛错；启动回补只在**没有失败**时写 `backfill` 标记；
4. 20:00 告警要能报「基础采集未完成」；
5. 基础采集没采全时只跳推送（简报 / 形态），日线/派生步骤照跑；
6. reasons / themes 的 0 行不能记成 ok（用站内涨停池家数守），lhb 空返回记 partial；
7. `kaipanhong` 的「今天」走 `today_cn()`、`collect_dde` 的收盘守卫走 `now_cn()`；
8. B1 盘中预览进 `collect_guard`、`_fetch_snapshot` 用共享 iFinD 客户端；
9. 补采日志里的「本次失败」要排除 skipped。

测试写法照现有 `tests/` 的习惯：内存 SQLite + `monkeypatch` 掉各模块的
`session_scope`，采集器用 `__new__` 构造（不建 iFinD / akshare 客户端、不发任何请求）。
"""

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.jobs import alerts, collect_dde, collect_daily, collect_funds, scheduler
from app.jobs import collect_reasons, collect_themes, intraday_b1
from app.jobs.collect_daily import DailyCollector
from app.models import (
    Base,
    CollectLog,
    EtfShare,
    IndexDaily,
    LimitPool,
    MarketSentiment,
    StockDde,
    TradeCalendar,
)
from app.services.usage import QuotaLevel
from app.sources import kaipanhong
from app.timeutil import CST

DAY = date(2026, 10, 21)
PREV = date(2026, 10, 20)
TODAY = DAY


# --------------------------------------------------------------------------- 公共
def _engine():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return engine


def _patch_scope(monkeypatch, module, engine) -> None:
    """把某个模块里的 `session_scope` 换成指向内存库的版本。"""

    @contextmanager
    def scope():
        with Session(engine) as session:
            yield session
            session.commit()

    monkeypatch.setattr(module, "session_scope", scope)


def _add_log(engine, task: str, status: str, *, day: date = DAY, rows: int = 1) -> None:
    with Session(engine) as session:
        session.add(CollectLog(trade_date=day, task=task, status=status, rows=rows))
        session.commit()


def _add_pool(engine, code: str, pool_type: str = "up", *, day: date = DAY) -> None:
    with Session(engine) as session:
        session.add(LimitPool(trade_date=day, code=code, pool_type=pool_type))
        session.commit()


def _add_sentiment(engine, *, day: date = DAY) -> None:
    with Session(engine) as session:
        session.add(MarketSentiment(trade_date=day))
        session.commit()


def _add_calendar(engine, *days: date) -> None:
    with Session(engine) as session:
        session.add_all([TradeCalendar(trade_date=day) for day in days])
        session.commit()


def _count(engine, model) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def _collector(**attrs) -> DailyCollector:
    """不建客户端地造一个采集器（测试只调纯逻辑方法）。"""
    collector = DailyCollector.__new__(DailyCollector)
    collector.settings = None
    collector._step_note = None
    collector._step_status = None
    for name, value in attrs.items():
        setattr(collector, name, value)
    return collector


# --------------------------------------------------------------- #1 历史日 ETF 不回退
def _etf_patch(monkeypatch, called: list[str]):
    monkeypatch.setattr(collect_funds, "today_cn", lambda: DAY)
    monkeypatch.setattr(
        collect_funds,
        "_collect_etf_exchange",
        lambda target: called.append(f"exchange:{target}") or 100,
    )
    monkeypatch.setattr(
        collect_funds,
        "_collect_etf_ifind",
        lambda ak, ifind: called.append("ifind") or 1,
    )


def test_collect_etf_history_target_raises_not_ready(monkeypatch):
    """历史日：抛 EtfNotReady（调用方记 skipped），**不许**碰 iFinD 兜底。"""
    called: list[str] = []
    _etf_patch(monkeypatch, called)

    with pytest.raises(collect_funds.EtfNotReady):
        collect_funds.collect_etf(None, None, trade_date=DAY - timedelta(days=1))

    assert called == []


def test_collect_etf_today_uses_exchange_source(monkeypatch):
    """当天：照常走交易所源（守卫不能把正常路径也挡掉）。"""
    called: list[str] = []
    _etf_patch(monkeypatch, called)

    assert collect_funds.collect_etf(None, None, trade_date=DAY) == 100
    assert called == [f"exchange:{DAY}"]


def test_daily_collector_etf_history_marks_skipped(monkeypatch):
    """`DailyCollector.collect_etf` 把 EtfNotReady 落成 skipped（不是 failed）。"""

    def _raise(ak, ifind, trade_date=None):
        raise collect_funds.EtfNotReady("历史日没有快照")

    monkeypatch.setattr(collect_funds, "collect_etf", _raise)
    collector = _collector(ak=None, ifind=None)

    assert collector.collect_etf(DAY - timedelta(days=1)) == 0
    assert collector._step_status == "skipped"
    assert "历史日没有快照" in (collector._step_note or "")


def _patch_stale_etf(monkeypatch, *, snap: date, complete: bool):
    """深交所快照停在 `snap`、且该日快照是否完整由 `complete` 决定。"""
    engine = _engine()
    _patch_scope(monkeypatch, collect_funds, engine)
    monkeypatch.setattr(
        collect_funds.etf_exchange, "szse_snapshot", lambda: (snap, 1, 1660)
    )
    monkeypatch.setattr(
        collect_funds, "_etf_snapshot_complete", lambda session, day: (1660, complete)
    )
    monkeypatch.setattr(collect_funds, "_heal_etf_quotes", lambda day, limit=2: None)


def test_etf_stale_snapshot_with_previous_complete_raises(monkeypatch):
    """快照还停在昨天、昨天那份已完整 → 必须抛 EtfNotReady（2026-10-22 复查 P1-1）。

    改之前这里直接 `return total`，调用方把今天的 `etf` 记成 **ok**，22:35 的终局
    （`skip_done=True`）看到 ok 就跳过 —— 今天照样 0 行，日志却是绿的。
    """
    _patch_stale_etf(monkeypatch, snap=PREV, complete=True)

    with pytest.raises(collect_funds.EtfNotReady) as exc:
        collect_funds._collect_etf_exchange(DAY)

    assert PREV.isoformat() in str(exc.value)


def test_etf_current_snapshot_complete_is_ok(monkeypatch):
    """快照日就是目标日且已完整 → 正常返回（不能把正常路径也判成「没发布」）。"""
    _patch_stale_etf(monkeypatch, snap=DAY, complete=True)

    assert collect_funds._collect_etf_exchange(DAY) == 1660


def test_etf_stale_incomplete_snapshot_writes_then_raises(monkeypatch):
    """快照停在昨天、昨天还不完整 → 先把昨天补完，**今天仍要记 skipped**。

    （复查 3c02e21 补漏）漏掉这一支的后果：每天补的都是前一天、今天被记成 ok，
    22:35 终局看到 ok 就跳过 —— 数据永远晚一天，日志却一直是绿的。
    """
    engine = _engine()
    _patch_scope(monkeypatch, collect_funds, engine)
    monkeypatch.setattr(
        collect_funds.etf_exchange, "szse_snapshot", lambda: (PREV, 1, 1660)
    )
    monkeypatch.setattr(
        collect_funds, "_etf_snapshot_complete", lambda session, day: (0, False)
    )
    monkeypatch.setattr(collect_funds, "_etf_quotes", lambda day: {})
    monkeypatch.setattr(collect_funds, "_heal_etf_quotes", lambda day, limit=2: None)
    monkeypatch.setattr(collect_funds, "ETF_EXCHANGE_MIN_ROWS", 1)
    monkeypatch.setattr(
        collect_funds.etf_exchange,
        "fetch_shares",
        lambda day: [
            {"trade_date": PREV, "code": "510300", "name": "沪深300ETF", "shares": 1.0},
            {"trade_date": PREV, "code": "159915", "name": "创业板ETF", "shares": 2.0},
        ],
    )

    with pytest.raises(collect_funds.EtfNotReady) as exc:
        collect_funds._collect_etf_exchange(DAY)

    assert PREV.isoformat() in str(exc.value)
    assert DAY.isoformat() in str(exc.value)
    # 昨天那份照样落库 —— 抛错只改「今天」的状态标记，不影响已取到的数据
    assert _count(engine, EtfShare) == 2


# ------------------------------------------------- #2 三池纳入完成判据（skipped→非 failed）
def test_has_collected_skipped_pool_is_not_failed(monkeypatch):
    """超期 `skipped` 不算失败 —— 否则历史日每次重启都会重跑整套 run()。"""
    engine = _engine()
    _patch_scope(monkeypatch, collect_daily, engine)
    _add_sentiment(engine)
    for task in collect_daily.RUN_ALWAYS_TASKS:
        _add_log(engine, task, "ok")
    _add_log(engine, "pool_up", "ok")
    _add_log(engine, "pool_down", "skipped")
    _add_log(engine, "pool_broken", "ok")

    assert _collector().has_collected(DAY) is True


def test_has_collected_failed_pool_blocks_completion(monkeypatch):
    """跌停池失败 → 这天判「没采完」（否则缺口永远没人补）。"""
    engine = _engine()
    _patch_scope(monkeypatch, collect_daily, engine)
    _add_sentiment(engine)
    for task in collect_daily.RUN_ALWAYS_TASKS:
        _add_log(engine, task, "ok")
    _add_log(engine, "pool_up", "ok")
    _add_log(engine, "pool_down", "ok")
    _add_log(engine, "pool_broken", "ok")
    _add_log(engine, "pool_down", "failed")

    assert _collector().has_collected(DAY) is False


# ----------------------------------------- #3 backfill_index 空返回 / 回补标记
class _FakeIfind:
    """只实现 `index_data`；`rows_by_name` 命中名称才给数据。"""

    def __init__(self, rows_by_name: dict[str, list[dict]]) -> None:
        self.rows_by_name = rows_by_name
        self.queries: list[str] = []

    def index_data(self, query: str):
        self.queries.append(query)
        for name, rows in self.rows_by_name.items():
            if name in query:
                return "", rows
        return "", []


def _index_row(*, day: date = DAY, code: str = "000001.SH") -> dict:
    return {
        "日期": day.strftime("%Y%m%d"),
        "证券代码": code,
        "证券简称": "上证指数",
        "收盘价": 3500.0,
        "涨跌幅": 0.5,
        "成交额": 5.0e11,
    }


def test_backfill_index_raises_when_all_empty(monkeypatch):
    """问了 11 个指数却一行都没回来 → 抛 IfindError（不能记 ok）。"""
    engine = _engine()
    _patch_scope(monkeypatch, collect_daily, engine)
    _add_calendar(engine, DAY)
    ifind = _FakeIfind({})
    collector = _collector(ifind=ifind)

    with pytest.raises(collect_daily.IfindError):
        collector.backfill_index(DAY, DAY)

    assert len(ifind.queries) == len(collect_daily.INDEX_NAMES)


def test_backfill_index_accepts_partial_data(monkeypatch):
    """只要有**一个**指数取到数据就算成功（个别主体 0 行是正常的）。"""
    engine = _engine()
    _patch_scope(monkeypatch, collect_daily, engine)
    _add_calendar(engine, DAY)
    ifind = _FakeIfind({"上证指数": [_index_row(day=DAY)]})
    collector = _collector(ifind=ifind)

    written = collector.backfill_index(DAY, DAY)

    assert written == 1
    assert _count(engine, IndexDaily) == 1


def _patch_backfill_env(monkeypatch, engine, steps: dict[str, dict]):
    """把 `_catch_up_backfill` 的依赖全部换掉（不碰真库、不碰真采集器）。"""
    _patch_scope(monkeypatch, scheduler, engine)

    class _FakeCollector:
        def __init__(self, settings=None) -> None:
            self.settings = settings

        def has_collected(self, day: date) -> bool:
            return False

        def run_backfill(self, day: date) -> dict:
            return {"trade_date": day.isoformat(), "steps": steps}

    monkeypatch.setattr(scheduler, "DailyCollector", _FakeCollector)
    monkeypatch.setattr(scheduler, "quota_level", lambda settings=None: QuotaLevel.NORMAL)
    monkeypatch.setattr(scheduler, "_recent_trade_days", lambda end, count: [DAY])
    monkeypatch.setattr(scheduler, "_logged_tasks", lambda day, tasks, status: set())
    sched = scheduler.DailyScheduler.__new__(scheduler.DailyScheduler)
    sched.settings = object()
    return sched


def test_catch_up_backfill_no_marker_on_failure(monkeypatch):
    """有失败步骤 → **不写** backfill 标记（否则这天被永久封口、再也不重试）。"""
    engine = _engine()
    sched = _patch_backfill_env(
        monkeypatch,
        engine,
        {"index": {"status": "failed"}, "sentiment": {"status": "skipped"}},
    )

    sched._catch_up_backfill()

    assert _count(engine, CollectLog) == 0


def test_catch_up_backfill_writes_marker_when_ok(monkeypatch):
    """全是 ok/skipped → 写一条 ok 标记收口（下次启动不再重复烧配额）。"""
    engine = _engine()
    sched = _patch_backfill_env(
        monkeypatch,
        engine,
        {"index": {"status": "ok"}, "sectors": {"status": "skipped"}},
    )

    sched._catch_up_backfill()

    with Session(engine) as session:
        rows = list(session.scalars(select(CollectLog)))
    assert len(rows) == 1
    assert rows[0].task == scheduler.BACKFILL_TASK
    assert rows[0].status == "ok"


# --------------------------------------------------- #5 基础没采全时只跳推送
TAIL_STEPS = (
    "_collect_kline",
    "_backfill_kline",
    "_collect_fear_greed",
    "_collect_breadth",
    "_collect_avg_price",
    "_collect_futures_basis",
    "_collect_erp",
    "_collect_qvix",
    "_scan_patterns",
    "_backfill_hit_kline",
    "_backfill_hit_dde",
    "_scan_dde",
    "_collect_board_flow",
    "_maybe_remind_calibration",
)
PUSH_STEPS = ("_push_brief", "_push_pattern")


def _patch_tail(monkeypatch, called: list[str]) -> scheduler.DailyScheduler:
    sched = scheduler.DailyScheduler.__new__(scheduler.DailyScheduler)
    sched.settings = object()
    sched._tail_failures = []
    for name in TAIL_STEPS + PUSH_STEPS:
        monkeypatch.setattr(
            sched, name, (lambda key: lambda *a, **k: called.append(key))(name)
        )
    monkeypatch.setattr(scheduler, "_mark_tail", lambda *a, **k: None)
    return sched


def test_run_tail_allow_push_false_skips_only_pushes(monkeypatch):
    """基础采集没采全：只跳过两步推送，其余照跑，**并记一笔 push_deferred**。

    不记的话其它步骤都成功时这条尾巴会被 `_mark_tail` 写成 ok，而 `tail_done` 把 ok
    当「跑过」→ 之后再没人补推（2026-10-22 复查 P1-2）。
    """
    called: list[str] = []
    sched = _patch_tail(monkeypatch, called)

    sched._run_tail(DAY, "测试", allow_push=False)

    assert called == list(TAIL_STEPS)
    assert not set(PUSH_STEPS) & set(called)
    assert "push_deferred" in sched._tail_failures


def test_run_tail_allow_push_true_pushes(monkeypatch):
    """基础数据完整（默认）时照旧推送。"""
    called: list[str] = []
    sched = _patch_tail(monkeypatch, called)

    sched._run_tail(DAY, "测试")

    assert set(PUSH_STEPS) <= set(called)
    assert "push_deferred" not in sched._tail_failures


def _patch_push_ready(
    monkeypatch, *, collected: bool, done: bool, pushed: bool
) -> tuple[scheduler.DailyScheduler, list[str], SimpleNamespace]:
    """造一个 `_push_if_ready` 的环境：三处判据（已采齐 / 尾部跑过 / 已推过）都可控。"""
    sched = scheduler.DailyScheduler.__new__(scheduler.DailyScheduler)
    sched.settings = object()
    sched._tail_failures = []
    called: list[str] = []
    monkeypatch.setattr(sched, "_push_brief", lambda day: called.append("brief"))
    monkeypatch.setattr(
        sched, "_push_pattern", lambda pattern, day: called.append(f"pattern:{pattern}")
    )
    monkeypatch.setattr(scheduler, "tail_done", lambda day: done)
    monkeypatch.setattr(
        "app.jobs.push_brief.already_pushed",
        lambda day, task=None: pushed,
    )
    collector = SimpleNamespace(has_collected=lambda day: collected)
    return sched, called, collector


def test_push_if_ready_pushes_after_base_completed(monkeypatch):
    """15:05 被推迟、19:30 基础数据已齐 → 补推一次（简报 + 形态）。"""
    sched, called, collector = _patch_push_ready(
        monkeypatch, collected=True, done=True, pushed=False
    )

    sched._push_if_ready(collector, DAY, "测试")

    assert called == ["brief", "pattern:limit_surge_flat"]


def test_push_if_ready_skips_when_base_still_incomplete(monkeypatch):
    """基础数据还是没采齐 → 不推（发出去只会是一份缺段的消息）。"""
    sched, called, collector = _patch_push_ready(
        monkeypatch, collected=False, done=True, pushed=False
    )

    sched._push_if_ready(collector, DAY, "测试")

    assert called == []


def test_push_if_ready_skips_when_tail_not_done(monkeypatch):
    """尾部压根没跑过 → 让上面那段 `_run_tail` 去推，这里不重复。"""
    sched, called, collector = _patch_push_ready(
        monkeypatch, collected=True, done=False, pushed=False
    )

    sched._push_if_ready(collector, DAY, "测试")

    assert called == []


def test_push_if_ready_does_not_repeat(monkeypatch):
    """已经推成功过（`already_pushed` 只认 ok）→ 后续趟次（22:35 / 重启）不再发。"""
    sched, called, collector = _patch_push_ready(
        monkeypatch, collected=True, done=True, pushed=True
    )

    sched._push_if_ready(collector, DAY, "测试")

    assert called == []


# ------------------------------------------- #6 收盘后重算恐贪（等两融入库）
def test_run_late_recomputes_fear_greed_before_pushes(monkeypatch):
    """17:30 / 19:30 / 22:35 这几趟要在两融入库之后重算恐贪（2026-10-31 加）。

    15:05 那趟算出的当天读数缺两融（当天晚上才发布），不能只靠它；重算必须排在
    推送之前，顺序错了当晚的页面与简报还是旧分数。
    """
    sched = scheduler.DailyScheduler.__new__(scheduler.DailyScheduler)
    sched.settings = object()
    sched._tail_failures = []
    called: list[str] = []
    monkeypatch.setattr(sched, "_catch_up_late", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "tail_done", lambda day: True)
    monkeypatch.setattr(sched, "_push_if_ready", lambda *a, **k: called.append("push"))
    monkeypatch.setattr(sched, "_push_cb_redeem", lambda *a, **k: called.append("cb"))
    monkeypatch.setattr(
        sched,
        "_collect_fear_greed",
        lambda day, **k: called.append(f"fear:{k.get('record_failure', True)}"),
    )
    collector = SimpleNamespace(
        is_trade_day=lambda day: True,
        run_late=lambda day, skip_done=False: {"steps": {}, "trade_date": day},
    )

    sched._run_late_unguarded(collector, DAY, "测试")

    # `record_failure=False`：这里不是尾部链，失败不该记进 `_tail_failures`
    assert called == ["fear:False", "push", "cb"]


def test_late_fear_greed_failure_keeps_tail_failures_clean(monkeypatch):
    """收盘后那趟重算失败只进日志；尾部链那趟（默认参数）仍要登记，好记 partial。"""
    sched = scheduler.DailyScheduler.__new__(scheduler.DailyScheduler)
    sched._tail_failures = ["stale"]

    def boom():
        raise RuntimeError("数据库锁了")

    monkeypatch.setattr("app.jobs.collect_fear_greed.refresh", boom)

    sched._collect_fear_greed(DAY, record_failure=False)
    assert sched._tail_failures == ["stale"]

    sched._collect_fear_greed(DAY)
    assert sched._tail_failures == ["stale", "fear_greed"]


def _patch_run_daily(monkeypatch, collected_after: bool):
    """造一个假的 `_run_daily` 环境，返回捕获 `allow_push` 的字典。"""

    class _FakeCollector:
        def __init__(self, settings=None) -> None:
            self.settings = settings
            self.collected = False

        def is_trade_day(self, day: date) -> bool:
            return True

        def has_collected(self, day: date) -> bool:
            return self.collected

        def run(self, day: date) -> dict:
            self.collected = collected_after
            status = "ok" if collected_after else "failed"
            return {
                "trade_date": day.isoformat(),
                "steps": {"index": {"status": status, "rows": 0}},
            }

    monkeypatch.setattr(scheduler, "today_cn", lambda: DAY)
    monkeypatch.setattr(scheduler, "DailyCollector", _FakeCollector)
    monkeypatch.setattr(scheduler, "tail_done", lambda day: False)
    captured: dict = {}
    sched = scheduler.DailyScheduler.__new__(scheduler.DailyScheduler)
    sched.settings = object()
    sched._last_run = None
    sched._last_result = None
    sched._tail_failures = []
    monkeypatch.setattr(
        sched,
        "_run_tail",
        lambda day, reason, **kwargs: captured.update({"day": day, **kwargs}),
    )
    return sched, captured


def test_run_daily_disallows_push_when_base_incomplete(monkeypatch):
    """15:05 基础步骤失败 → 尾部 allow_push=False（简报不发、不记 ok）。"""
    sched, captured = _patch_run_daily(monkeypatch, collected_after=False)

    sched._run_daily("测试")

    assert captured["day"] == DAY
    assert captured["allow_push"] is False


def test_run_daily_allows_push_when_base_ok(monkeypatch):
    """基础采集成功 → 尾部 allow_push=True（正常日子照发简报）。"""
    sched, captured = _patch_run_daily(monkeypatch, collected_after=True)

    sched._run_daily("测试")

    assert captured["allow_push"] is True


# --------------------------------- #6 reasons / themes / lhb 的 0 行不能记成功
class _FakeReasonSource:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def limit_reasons(self, day: date) -> list[dict]:
        return list(self.rows)


def _reason_collector(rows: list[dict]) -> collect_reasons.ReasonCollector:
    collector = collect_reasons.ReasonCollector.__new__(collect_reasons.ReasonCollector)
    collector.settings = None
    collector.source = _FakeReasonSource(rows)
    return collector


def test_reasons_zero_rows_with_pool_raises(monkeypatch):
    """站内涨停池有票、同花顺却 0 行 → 抛错（不能记 ok）。"""
    engine = _engine()
    _patch_scope(monkeypatch, collect_reasons, engine)
    _add_pool(engine, "600000")

    with pytest.raises(RuntimeError):
        _reason_collector([]).collect(DAY)


def test_reasons_zero_rows_without_pool_is_normal(monkeypatch):
    """涨停池也是 0（非交易日 / 真没有涨停）→ 正常空返回，不抛。"""
    engine = _engine()
    _patch_scope(monkeypatch, collect_reasons, engine)

    assert _reason_collector([]).collect(DAY) == 0


def test_reasons_rows_still_work(monkeypatch):
    """有数据时照常落库（守卫不能把正常路径也挡掉）。"""
    engine = _engine()
    _patch_scope(monkeypatch, collect_reasons, engine)
    _add_pool(engine, "600000")

    written = _reason_collector(
        [{"code": "600000", "reason": "房地产+城市更新"}]
    ).collect(DAY)

    assert written == 1


class _FakeLadder:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def limit_up_ladder(self, day: date) -> list[dict]:
        return list(self.rows)


def _theme_collector(rows: list[dict]) -> collect_themes.ThemeCollector:
    collector = collect_themes.ThemeCollector.__new__(collect_themes.ThemeCollector)
    collector.settings = None
    collector.kph = _FakeLadder(rows)
    return collector


def test_themes_zero_rows_with_pool_raises(monkeypatch):
    """开盘红天梯 0 行、站内涨停池有票 → 抛错（否则 `_catch_up_late` 会当补完了）。"""
    engine = _engine()
    _patch_scope(monkeypatch, collect_themes, engine)
    _add_pool(engine, "600000")

    with pytest.raises(RuntimeError):
        _theme_collector([]).collect(DAY)


def test_themes_zero_rows_without_pool_is_normal(monkeypatch):
    engine = _engine()
    _patch_scope(monkeypatch, collect_themes, engine)

    assert _theme_collector([]).collect(DAY) == 0


def test_themes_rows_still_work(monkeypatch):
    engine = _engine()
    _patch_scope(monkeypatch, collect_themes, engine)
    _add_pool(engine, "600000")

    written = _theme_collector(
        [{"code": "600000", "board_name": "芯片", "ladder_date": DAY}]
    ).collect(DAY)

    assert written == 1


def test_collect_lhb_empty_marks_partial(monkeypatch):
    """龙虎榜 0 行 → 步骤记 `partial`（可见、但不阻塞完成判据）。"""
    engine = _engine()
    _patch_scope(monkeypatch, collect_daily, engine)
    collector = _collector(ak=SimpleNamespace(lhb=lambda day: []))

    row = collector._step(DAY, "lhb", lambda: collector.collect_lhb(DAY))

    assert row["status"] == "partial"
    assert "龙虎榜" in (row["message"] or "")
    with Session(engine) as session:
        status = session.scalar(
            select(CollectLog.status).where(CollectLog.task == "lhb")
        )
    assert status == "partial"


def test_collect_lhb_rows_are_ok(monkeypatch):
    """有行时照旧 ok（不能把正常的 15:05 采集判成 partial）。"""
    engine = _engine()
    _patch_scope(monkeypatch, collect_daily, engine)
    raw = [{"代码": "600000", "名称": "浦发银行", "上榜日": DAY.isoformat()}]
    collector = _collector(ak=SimpleNamespace(lhb=lambda day: raw))

    row = collector._step(DAY, "lhb", lambda: collector.collect_lhb(DAY))

    assert row["status"] == "ok"
    assert row["rows"] == 1


# ---------------------------------------------------- #4 告警覆盖基础采集
def _alerts_engine(monkeypatch, *, base_failed: tuple[str, ...] = ()):
    engine = _engine()
    _patch_scope(monkeypatch, alerts, engine)
    _add_calendar(engine, DAY)
    _add_log(engine, "push_brief", "ok")
    _add_log(engine, "daily_tail", "ok")
    _add_log(engine, "db_backup", "ok", day=PREV)
    for task in collect_daily.RUN_ALWAYS_TASKS:
        _add_log(engine, task, "failed" if task in base_failed else "ok")
    monkeypatch.setattr(alerts, "quota_status", lambda day, settings: {"level": 0})
    monkeypatch.setattr(
        "app.jobs.push_brief.send_markdown",
        lambda markdown, settings: {"ok": True},
    )
    settings = SimpleNamespace(
        alert_enabled=True,
        feishu_push_enabled=True,
        feishu_webhook_url="https://example.invalid/hook",
        feishu_target="",
    )
    return engine, settings


def test_alert_reports_failed_base_step(monkeypatch):
    """基础采集某步 failed → 告警带上「基础采集有步骤失败」。"""
    _engine_, settings = _alerts_engine(monkeypatch, base_failed=("sentiment",))

    result = alerts.check(DAY, settings)

    assert result["status"] == "sent"
    assert any("基础采集有步骤失败" in item and "sentiment" in item for item in result["issues"])


def test_alert_no_base_issue_when_all_ok(monkeypatch):
    """基础采集全 ok → 不产生基础采集告警（正常日子保持安静）。"""
    _engine_, settings = _alerts_engine(monkeypatch)

    result = alerts.check(DAY, settings)

    assert result["status"] == "ok"
    assert result["issues"] == []


# ------------------------------------- #7 今天/现在 走北京时间口径
class _FakeResponse:
    def raise_for_status(self) -> None:  # noqa: D401 - 假对象
        return None

    def json(self) -> dict:
        return {"errcode": 0}


def test_kaipanhong_post_uses_today_cn(monkeypatch):
    """`_post` 的实时/历史域名按 `today_cn()` 选，不看进程本地日期。"""
    source = kaipanhong.KaipanhongSource.__new__(kaipanhong.KaipanhongSource)
    source.settings = SimpleNamespace(
        http_timeout=1.0, http_retries=1, http_backoff=0.0, kph_rate_limit=1000.0
    )
    source._bucket = SimpleNamespace(acquire=lambda: None)
    monkeypatch.setattr(kaipanhong, "today_cn", lambda: DAY)
    urls: list[str] = []

    def _fake_post(url, **kwargs):
        urls.append(url)
        return _FakeResponse()

    monkeypatch.setattr(kaipanhong.requests, "post", _fake_post)

    source._post(DAY, {}, "今天")
    source._post(DAY - timedelta(days=1), {}, "历史")

    assert urls == [kaipanhong.HOST_REALTIME, kaipanhong.HOST_HISTORY]


def test_collect_dde_store_uses_now_cn(monkeypatch):
    """收盘守卫按北京时间判：盘中丢掉当天行，收盘后写入。"""
    engine = _engine()
    _patch_scope(monkeypatch, collect_dde, engine)
    _add_calendar(engine, DAY)
    monkeypatch.setattr(collect_dde, "today_cn", lambda: DAY)
    parsed = [{"trade_date": DAY, "code": "600000", "net_inflow": 1.0, "dde": 2.0}]

    monkeypatch.setattr(
        collect_dde,
        "now_cn",
        lambda: datetime(2026, 10, 21, 10, 0, tzinfo=CST),
    )
    assert collect_dde._store("600000", parsed, truncated=False, note="盘中") == 0
    assert _count(engine, StockDde) == 0

    monkeypatch.setattr(
        collect_dde,
        "now_cn",
        lambda: datetime(2026, 10, 21, 16, 0, tzinfo=CST),
    )
    assert collect_dde._store("600000", parsed, truncated=False, note="收盘后") == 1
    assert _count(engine, StockDde) == 1


# --------------------------------------------- #8 B1 预览的锁与共享客户端
def test_b1_intraday_skips_when_lock_busy(monkeypatch):
    """拿不到采集锁 → 直接跳过，不调用数据源。"""
    called: list[int] = []
    monkeypatch.setattr(
        "app.jobs.intraday_b1.run",
        lambda settings=None: called.append(1) or {"status": "sent"},
    )

    @contextmanager
    def _busy(who="采集"):
        raise collect_daily.CollectionBusy("占用中")
        yield  # pragma: no cover - 只为让它是生成器

    monkeypatch.setattr(scheduler, "collect_guard", _busy)
    sched = scheduler.DailyScheduler.__new__(scheduler.DailyScheduler)
    # `_run_b1_intraday` 会先看 `ifind_auth_token`（无 token 软跳过），桩里得有这个字段
    sched.settings = SimpleNamespace(ifind_auth_token="test")

    sched._run_b1_intraday()

    assert called == []


def test_b1_intraday_runs_inside_lock(monkeypatch):
    """正常拿到锁 → 照常跑一轮。"""
    called: list[int] = []
    monkeypatch.setattr(
        "app.jobs.intraday_b1.run",
        lambda settings=None: called.append(1) or {"status": "sent"},
    )

    @contextmanager
    def _guard(who="采集"):
        yield

    monkeypatch.setattr(scheduler, "collect_guard", _guard)
    sched = scheduler.DailyScheduler.__new__(scheduler.DailyScheduler)
    sched.settings = SimpleNamespace(ifind_auth_token="test")

    sched._run_b1_intraday()

    assert called == [1]


def test_b1_intraday_soft_skips_without_token(monkeypatch):
    """没配 token → 软跳过（一条 warning），不调用数据源、也不抛堆栈。"""
    called: list[int] = []
    monkeypatch.setattr(
        "app.jobs.intraday_b1.run",
        lambda settings=None: called.append(1) or {"status": "sent"},
    )

    sched = scheduler.DailyScheduler.__new__(scheduler.DailyScheduler)
    sched.settings = SimpleNamespace(ifind_auth_token="")

    sched._run_b1_intraday()

    assert called == []


def test_fetch_snapshot_uses_shared_client(monkeypatch):
    """`_fetch_snapshot` 必须用进程内共享的 iFinD 客户端（共享限速桶）。"""
    sentinel = object()
    seen: list[object] = []
    monkeypatch.setattr(intraday_b1, "get_shared_client", lambda settings=None: sentinel)

    def _fake_crawl(client, query_of, what):
        seen.append(client)
        return {}, 0

    monkeypatch.setattr(intraday_b1, "crawl_prefixes", _fake_crawl)

    rows, calls = intraday_b1._fetch_snapshot(DAY, None)

    assert rows == {}
    assert calls == 0
    assert seen == [sentinel]


# ------------------------------------------------- #9 补采日志排除 skipped
def test_split_step_statuses_excludes_skipped():
    failed, skipped = scheduler._split_step_statuses(
        {
            "lhb": {"status": "ok"},
            "themes": {"status": "skipped"},
            "etf": {"status": "skipped"},
            "index": {"status": "failed"},
        }
    )

    assert failed == ["index"]
    assert skipped == ["themes", "etf"]
