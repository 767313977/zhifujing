"""晚间补采（`run_late`）的去重：已 ok 的步骤不再重跑（2026-10-22 体检）。

背景：线上 10-21 一天重启 28 次（每次部署都重启，`_catch_up` 又会补这一趟），
整条晚间链被跑了 20 多轮 —— `edb/get_edb_data` 当天 52 次（正常 4 次），
配额直接被打到 9042/9000。所以「启动补采 / 回看补采」现在传 `skip_done=True`，
定时触发的那两趟仍然是全量重跑。这几条用例把两种语义都钉住。
"""

from contextlib import contextmanager
from datetime import date

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.jobs import collect_daily
from app.jobs.collect_daily import DailyCollector
from app.models import Base, CollectLog
from app.services.usage import QuotaLevel

DAY = date(2026, 10, 21)
LATE_STEP_METHODS = (
    "collect_lhb",
    "collect_lhb_institution",
    "collect_themes",
    "collect_activity",
    "collect_etf",
    "collect_margin",
    "collect_hsgt",
)


def _engine():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return engine


def _patch_scope(monkeypatch, engine) -> None:
    @contextmanager
    def scope():
        with Session(engine) as session:
            yield session
            session.commit()

    monkeypatch.setattr(collect_daily, "session_scope", scope)


def _collector(settings=None) -> DailyCollector:
    collector = DailyCollector.__new__(DailyCollector)
    collector.settings = settings
    collector._step_note = None
    collector._step_status = None
    return collector


def _add_log(engine, task: str, status: str) -> None:
    with Session(engine) as session:
        session.add(CollectLog(trade_date=DAY, task=task, status=status, rows=1))
        session.commit()


def _log_count(engine) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(CollectLog))


def test_step_skips_when_already_ok(monkeypatch):
    """当天已 ok → 不调用数据源、不写日志（否则日志会被重启刷爆）。"""
    engine = _engine()
    _patch_scope(monkeypatch, engine)
    _add_log(engine, "themes", "ok")
    calls: list[int] = []

    row = _collector()._step(DAY, "themes", lambda: calls.append(1) or 7, skip_if_ok=True)

    assert calls == []
    assert row["status"] == "skipped"
    assert row["rows"] == 0
    assert _log_count(engine) == 1


def test_ok_tasks_uses_latest_status(monkeypatch):
    """「最近一条 ok」才算完成：ok 之后又失败，真实状态是没采全。"""
    engine = _engine()
    _patch_scope(monkeypatch, engine)
    _add_log(engine, "themes", "ok")
    _add_log(engine, "themes", "failed")
    _add_log(engine, "lhb", "ok")

    tasks = _collector()._ok_tasks(DAY)

    assert tasks == {"lhb"}


def test_step_runs_when_skip_flag_off(monkeypatch):
    """`skip_if_ok=False`（定时触发那两趟）必须照旧全量重跑。"""
    engine = _engine()
    _patch_scope(monkeypatch, engine)
    _add_log(engine, "themes", "ok")
    calls: list[int] = []

    row = _collector()._step(DAY, "themes", lambda: calls.append(1) or 7)

    assert calls == [1]
    assert row["status"] == "ok"


def _prepare_run_late(monkeypatch, engine, ok_tasks: tuple[str, ...]) -> tuple[DailyCollector, list[str]]:
    _patch_scope(monkeypatch, engine)
    for task in ok_tasks:
        _add_log(engine, task, "ok")
    collector = _collector(settings=object())
    monkeypatch.setattr(collect_daily, "quota_level", lambda settings=None: QuotaLevel.NORMAL)
    monkeypatch.setattr(collector, "latest_trade_date", lambda: DAY)
    called: list[str] = []
    for name in LATE_STEP_METHODS:
        monkeypatch.setattr(
            collector, name, (lambda n: lambda _day: called.append(n) or 1)(name)
        )
    monkeypatch.setattr(
        "app.jobs.collect_futures_position.collect",
        lambda _day, _settings: called.append("collect_futures_position") or 1,
    )
    return collector, called


def test_run_late_skip_done_only_runs_missing(monkeypatch):
    """启动补采：已 ok 的三步跳过，只补没采到的（活动 / ETF / 期指 / 两融 / 北向）。"""
    engine = _engine()
    collector, called = _prepare_run_late(
        monkeypatch, engine, ("lhb", "lhb_institution", "themes")
    )

    result = collector.run_late(DAY, skip_done=True)

    assert result["steps"]["themes"]["status"] == "skipped"
    assert result["steps"]["lhb"]["status"] == "skipped"
    assert set(called) == {
        "collect_activity",
        "collect_etf",
        "collect_futures_position",
        "collect_margin",
        "collect_hsgt",
    }


def test_run_late_full_run_by_default(monkeypatch):
    """定时触发（默认 `skip_done=False`）：七步全跑，不因上次 ok 而跳过。"""
    engine = _engine()
    collector, called = _prepare_run_late(
        monkeypatch, engine, ("lhb", "lhb_institution", "themes")
    )

    result = collector.run_late(DAY)

    assert set(called) == set(LATE_STEP_METHODS) | {"collect_futures_position"}
    assert all(step["status"] == "ok" for step in result["steps"].values())
