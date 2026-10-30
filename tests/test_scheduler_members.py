"""次日 08:30 的「补采昨日成分股」：只补今天之前、非交易日不跑。

2026-10-30 补：同一天的记录若是 `empty`（当晚名单还没发布），早间这趟必须再试一次 ——
原先「有记录就跳过」会把它永久挡在门外，10-29 就是这样丢了一天成分股，
`sector_daily.leader_name`（页面上的「龙一」）跟着空了一整天。
"""

from datetime import date

import pytest

from app.jobs import scheduler as scheduler_module


@pytest.fixture()
def job():
    # 不跑 __init__（它会建调度器 / 连库），只测这两个方法的分支
    instance = scheduler_module.DailyScheduler.__new__(scheduler_module.DailyScheduler)
    instance.settings = None
    return instance


def test_morning_catch_up_skips_non_trade_day(job, monkeypatch):
    touches = []
    monkeypatch.setattr(scheduler_module, "is_trade_day", lambda day: False)
    monkeypatch.setattr(
        job, "_catch_up_members", lambda today, reason, **kw: touches.append((today, reason))
    )

    job._catch_up_members_morning()

    assert touches == []


def test_morning_catch_up_uses_today(job, monkeypatch):
    recorded = []
    monkeypatch.setattr(scheduler_module, "is_trade_day", lambda day: True)
    monkeypatch.setattr(scheduler_module, "today_cn", lambda: date(2026, 10, 27))
    monkeypatch.setattr(
        job,
        "_catch_up_members",
        lambda today, reason, **kw: recorded.append((today, reason, kw)),
    )

    job._catch_up_members_morning()

    # `_catch_up_members` 内部只遍历「今天之前」（`_recent_trade_days` 不含 end），
    # 所以这里传今天不会给今天写 collect_log；`retry_non_ok=True` 让它对前一天的
    # empty 记录再试一次
    assert recorded == [(date(2026, 10, 27), "次日早间补采成分股", {"retry_non_ok": True})]


def _fake_collector(calls: list[date]):
    class FakeCollector:
        def __init__(self, settings):
            self.settings = settings

        def collect_all_members(self, day, **_kwargs):
            calls.append(day)
            return {"boards": 0, "written": 0, "failed": 0, "aborted": True}

    return FakeCollector


def test_skips_day_that_already_succeeded(job, monkeypatch):
    """有 ok 记录就不重跑，哪怕这趟允许重试非 ok 的日子。"""
    day = date(2026, 10, 29)
    monkeypatch.setattr(scheduler_module, "_recent_trade_days", lambda end, n: [day])
    monkeypatch.setattr(scheduler_module, "_logged_tasks", lambda d, t, s: {"members"})
    calls: list[date] = []
    monkeypatch.setattr(scheduler_module, "SectorCollector", _fake_collector(calls))

    job._catch_up_members(date(2026, 10, 30), "测试", retry_non_ok=True)

    assert calls == []


def test_normal_run_keeps_skipping_empty(job, monkeypatch):
    """22:00 / 22:30 / 启动回看不重试 empty —— 否则每晚重打约 370 个板块。"""
    day = date(2026, 10, 29)
    monkeypatch.setattr(scheduler_module, "_recent_trade_days", lambda end, n: [day])
    monkeypatch.setattr(
        scheduler_module, "_logged_tasks", lambda d, t, s: set() if s == "ok" else {"members"}
    )
    calls: list[date] = []
    monkeypatch.setattr(scheduler_module, "SectorCollector", _fake_collector(calls))

    job._catch_up_members(date(2026, 10, 30), "测试")

    assert calls == []


def test_morning_retries_only_yesterday(job, monkeypatch):
    """前一天的 empty 记录要再试一次，但只限最近一个交易日（今天 10-30 → 只补 10-29）。"""
    yesterday = date(2026, 10, 29)
    older = date(2026, 10, 28)
    monkeypatch.setattr(scheduler_module, "_recent_trade_days", lambda end, n: [yesterday, older])
    monkeypatch.setattr(
        scheduler_module, "_logged_tasks", lambda d, t, s: set() if s == "ok" else {"members"}
    )
    calls: list[date] = []
    recorded: list[tuple[date, str]] = []
    monkeypatch.setattr(scheduler_module, "SectorCollector", _fake_collector(calls))
    monkeypatch.setattr(
        scheduler_module, "_record_log", lambda d, t, s, r, m: recorded.append((d, s))
    )

    job._catch_up_members(date(2026, 10, 30), "早间", retry_non_ok=True)

    assert calls == [yesterday]
    assert recorded == [(yesterday, "empty")]
