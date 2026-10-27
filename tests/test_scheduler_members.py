"""次日 08:30 的「补采昨日成分股」：只补今天之前，且非交易日不跑。"""

from datetime import date

import pytest

from app.jobs import scheduler as scheduler_module


@pytest.fixture()
def job():
    # 不跑 __init__（它会建调度器 / 连库），只测这两个方法的分支
    return scheduler_module.DailyScheduler.__new__(scheduler_module.DailyScheduler)


def test_morning_catch_up_skips_non_trade_day(job, monkeypatch):
    touches = []
    monkeypatch.setattr(scheduler_module, "is_trade_day", lambda day: False)
    monkeypatch.setattr(
        job, "_catch_up_members", lambda today, reason: touches.append((today, reason))
    )

    job._catch_up_members_morning()

    assert touches == []


def test_morning_catch_up_uses_today(job, monkeypatch):
    recorded = []
    monkeypatch.setattr(scheduler_module, "is_trade_day", lambda day: True)
    monkeypatch.setattr(scheduler_module, "today_cn", lambda: date(2026, 10, 27))
    monkeypatch.setattr(
        job, "_catch_up_members", lambda today, reason: recorded.append((today, reason))
    )

    job._catch_up_members_morning()

    # `_catch_up_members` 内部只遍历「今天之前」（`_recent_trade_days` 不含 end），
    # 所以这里传今天不会给今天写 collect_log
    assert recorded == [(date(2026, 10, 27), "次日早间补采成分股")]
