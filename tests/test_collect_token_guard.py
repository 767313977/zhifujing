"""未配置 IFIND_AUTH_TOKEN 时的软跳过（任务 5 的验收口径）。

期望：定时任务只留**一条**可读 warning 后正常返回，不抛异常、不打堆栈，
也不能因为缺 token 把调度线程带崩。`IfindClient` 在构造时就抛 `IfindError`，
入口（日采 / 收盘后补采 / 成分股预取）都捕获它并转成 warning —— 前两个在这里钉住，
成分股预取走的是同一个 `DailyCollector(...)` 构造 + `except IfindError`。
"""

import logging

from app.config import Settings
from app.jobs import scheduler as scheduler_mod


def _settings() -> Settings:
    return Settings(ifind_auth_token="", scheduler_enabled=False)


def _run_silently(method) -> str:
    records: list[logging.LogRecord] = []

    class _Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Grab(level=logging.DEBUG)
    logger = logging.getLogger(scheduler_mod.__name__)
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        method()  # 不碰网络：构造 DailyCollector 就抛 IfindError
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    return "\n".join(record.getMessage() for record in records)


def test_daily_collect_soft_skips_without_token():
    sched = scheduler_mod.DailyScheduler(_settings())
    text = _run_silently(sched._run_daily)
    assert "跳过" in text
    assert "IFIND_AUTH_TOKEN" in text
    assert "Traceback" not in text


def test_late_collect_soft_skips_without_token():
    sched = scheduler_mod.DailyScheduler(_settings())
    text = _run_silently(sched._run_late)
    assert "跳过" in text
    assert "IFIND_AUTH_TOKEN" in text
    assert "Traceback" not in text
