"""定时采集任务。

交易日 15:05 自动采集当日数据。这个时刻是为了等收盘数据稳定。

另有两处针对「本机不常开」的处理：
- **启动补采**：错过 15:05 才开机时，启动后在后台补跑一次
- **错过触发的宽限**：misfire_grace_time 一小时内仍会补跑，且 coalesce 保证只跑一次

不要用多 worker 启动本服务：每个 worker 会各自拉起一个调度器，
同一时刻会重复采集。默认单 worker 运行即符合预期。
"""

import logging
import threading
from datetime import date, datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import Settings, get_settings
from app.jobs.collect_daily import CollectionBusy, DailyCollector, collect_guard
from app.sources.ifind import IfindError

logger = logging.getLogger(__name__)

TIMEZONE = "Asia/Shanghai"
JOB_ID = "collect_daily"


class DailyScheduler:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._scheduler: BackgroundScheduler | None = None
        self._last_run: datetime | None = None
        self._last_result: dict | None = None

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        if not self.settings.scheduler_enabled:
            logger.info("定时采集已关闭（scheduler_enabled=false）")
            return
        if self._scheduler is not None:
            return

        scheduler = BackgroundScheduler(timezone=TIMEZONE)
        scheduler.add_job(
            self._run_daily,
            CronTrigger(
                day_of_week="mon-fri",
                hour=self.settings.collect_hour,
                minute=self.settings.collect_minute,
                timezone=TIMEZONE,
            ),
            id=JOB_ID,
            name="交易日收盘采集",
            replace_existing=True,
            # 机器休眠等原因错过触发点，一小时内仍补跑；错过多次只跑一次
            misfire_grace_time=3600,
            coalesce=True,
        )
        scheduler.start()
        self._scheduler = scheduler
        logger.info(
            "定时采集已启动：交易日 %02d:%02d",
            self.settings.collect_hour,
            self.settings.collect_minute,
        )

        if self.settings.catchup_on_start:
            self._catch_up()

    def shutdown(self) -> None:
        if self._scheduler is None:
            return
        self._scheduler.shutdown(wait=False)
        self._scheduler = None
        logger.info("定时采集已停止")

    # -------------------------------------------------------------------- 状态

    def status(self) -> dict:
        job = self._scheduler.get_job(JOB_ID) if self._scheduler else None
        next_run = getattr(job, "next_run_time", None) if job else None
        return {
            "enabled": self.settings.scheduler_enabled,
            "running": bool(self._scheduler and self._scheduler.running),
            "collect_time": (
                f"{self.settings.collect_hour:02d}:{self.settings.collect_minute:02d}"
            ),
            "catchup_on_start": self.settings.catchup_on_start,
            "next_run_time": next_run.isoformat() if next_run else None,
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "last_result": self._last_result,
        }

    # -------------------------------------------------------------------- 执行

    def _run_daily(self, reason: str = "定时采集") -> None:
        today = date.today()
        try:
            collector = DailyCollector(self.settings)
        except IfindError as exc:
            logger.warning("%s 跳过：%s", reason, exc)
            return

        if not collector.is_trade_day(today):
            logger.info("%s 跳过：%s 不是交易日", reason, today)
            return
        if collector.has_collected(today):
            logger.info("%s 跳过：%s 已有数据", reason, today)
            return

        try:
            with collect_guard(reason):
                result = collector.run(today)
        except CollectionBusy as exc:
            logger.warning("%s 跳过：%s", reason, exc)
            return
        except Exception:  # noqa: BLE001 - 定时任务绝不能因异常中断调度
            logger.exception("%s 失败", reason)
            return

        self._last_run = datetime.now()
        self._last_result = result
        failed = [
            name
            for name, step in result.get("steps", {}).items()
            if step.get("status") != "ok"
        ]
        if failed:
            logger.warning("%s 完成但部分步骤失败：%s", reason, failed)
        else:
            logger.info("%s 完成：%s", reason, result.get("trade_date"))

    def _catch_up(self) -> None:
        """启动补采：本机不常开，错过采集时刻后开机也要补上。

        放后台线程跑，否则会阻塞服务启动好几秒。
        """
        now = datetime.now()
        if (now.hour, now.minute) < (
            self.settings.collect_hour,
            self.settings.collect_minute,
        ):
            return  # 还没到今天该采集的时刻，交给调度器即可

        logger.info("已过采集时刻，后台检查是否需要补采")
        thread = threading.Thread(
            target=self._run_daily,
            kwargs={"reason": "启动补采"},
            name="collect-catchup",
            daemon=True,
        )
        thread.start()


_scheduler: DailyScheduler | None = None


def start_scheduler() -> DailyScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = DailyScheduler()
    _scheduler.start()
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown()
        _scheduler = None


def get_scheduler() -> DailyScheduler | None:
    return _scheduler
