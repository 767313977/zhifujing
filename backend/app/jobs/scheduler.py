"""定时采集任务。

交易日 **17:30** 自动采集当日数据。时刻不是「收盘后越快越好」：
龙虎榜与机构席位（东财 datacenter）要等收盘后一段时间才发布，而采集是按
「当天」取的 —— 15:05 那一轮取回来是空的，且没人会回头补（实测 15:25 空、
17:05 有 42 行）。时刻定义在 `config.collect_hour/collect_minute`。

另有两处针对「本机不常开」的处理：
- **启动补采**：错过采集时刻才开机时，启动后在后台补跑一次
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
        else:
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

        # 推送与采集解耦：上面「已有数据」分支会跳过采集，但简报该发还是要发 ——
        # 否则盘中手动采过一次，收盘后就不会再有简报了。
        self._collect_kline(today)
        # 历史回补排在**形态扫描之前**：它只花固定几十次调用，
        # 而形态扫描是全市场日线的下游，先让历史落库再算，两者不抢
        self._backfill_kline(today)
        self._scan_patterns(today)
        self._push_brief(today)
        # 单独推送的形态：加新形态就在 `PUSH_PATTERNS` 里登记，然后在这里补一行
        self._push_pattern("limit_surge_flat", today)
        self._push_pattern("oneil_breakout", today)
        # DDE 扫描排最后：它是这条链上**唯一要花十几二十次调用**的一步（前缀 × 单日），
        # 前面几步里有零配额的，先跑完再说
        self._scan_dde(today)
        self._maybe_remind_calibration(today)

    def _scan_patterns(self, trade_date: date) -> None:
        """全市场形态扫描。

        零 iFinD 调用、0.7 秒跑完，所以它是最便宜的一环，失败也不必重试 ——
        每天跟一轮就行，漏了第二天自然会有新结果。
        """
        from app.jobs.scan_patterns import scan

        try:
            result = scan(trade_date, self.settings)
        except Exception:  # noqa: BLE001 - 形态是增强，不该影响简报与采集
            logger.exception("形态扫描失败")
            return
        logger.info(
            "形态扫描完成：%s 条命中 / %s 只票，用时 %ss",
            result.get("rows"),
            result.get("codes"),
            result.get("cost_seconds"),
        )

    def _collect_kline(self, trade_date: date) -> None:
        """全市场日线：按需重建股票池，再做当日增量。

        刻意**不放进** `DailyCollector.run()`：那是「手动采集」和回补的入口，
        在那里挂几十次 iFinD 调用会让每次手工补数都变得很重。

        失败只记日志。日线是形态选股的地基，但它塌了不该影响已经采到的基础数据。
        """
        from app.jobs.collect_kline import KlineCollector, has_bars
        from app.jobs.collect_universe import UniverseCollector

        if has_bars(trade_date):
            logger.info("日线 %s 已在库里，跳过", trade_date)
            return

        try:
            # 池子七天重建一次，平时这里是跳过
            pool = UniverseCollector(self.settings).collect(trade_date)
            logger.info("股票池：%s（%s）", pool.get("status"), pool.get("codes"))
        except Exception:  # noqa: BLE001 - 建池失败不该挡住日线：旧池还能用
            logger.exception("股票池重建失败，沿用旧池")

        try:
            result = KlineCollector(self.settings).collect(trade_date)
        except Exception:  # noqa: BLE001 - 日线失败不影响已经采到的基础数据
            logger.exception("日线采集失败")
            return
        if result.get("status") == "skipped":
            logger.info("日线跳过：%s", result.get("reason"))
        else:
            logger.info(
                "日线完成：采 %s 天 / %s 行 / %s 次调用，用时 %ss",
                len(result.get("fetched_days") or []),
                result.get("rows"),
                result.get("calls"),
                result.get("cost_seconds"),
            )

    def _backfill_kline(self, trade_date: date) -> None:
        """每天往前啃一小段历史（默认 4 个交易日）。

        为什么要有这一步：2 年窗口 ≈ 500 天 × 14 次 = **7000 次调用**，
        一轮 5000 的月度配额装不下，而历史恰恰是「越早的越补不回来」——
        只能拆成每天一小段，跨几个配额周期慢慢铺满。补完之后
        `_needs_day` 全为假，这一步就自动变成 0 调用（每天只多几条本地查询）。

        两条自我保护，见 `Settings` 里的注释：

        - **停手线比让路线更保守**（`kline_backfill_max_ratio` 0.74 vs 80%）：
          回补永远不会把当天的日线采集顶停 —— 历史慢慢补，当天的行情缺了就真缺了。
        - `max_days` 卡的是**本轮补几天**（窗口从旧到新推进），不是「只看最近几天」。
        """
        from app.jobs.collect_kline import KlineCollector
        from app.services.usage import quota_status

        usage = quota_status()
        if usage["usage_ratio"] >= self.settings.kline_backfill_max_ratio:
            logger.info(
                "历史回补跳过：本周期已用 %s/%s（%.0f%%），到 %.0f%% 就停手，等配额重置",
                usage["cycle_calls"],
                usage["monthly_quota"],
                usage["usage_ratio"] * 100,
                self.settings.kline_backfill_max_ratio * 100,
            )
            return

        try:
            result = KlineCollector(self.settings).collect(
                trade_date, full=True, max_days=self.settings.kline_backfill_days
            )
        except Exception:  # noqa: BLE001 - 历史回补失败不该影响别的步骤
            logger.exception("历史回补失败")
            return
        if result.get("status") == "skipped":
            logger.info("历史回补跳过：%s", result.get("reason"))
            return
        logger.info(
            "历史回补：往前补了 %s 天（%s 次调用），窗口内还剩 %s 天待补",
            len(result.get("fetched_days") or []),
            result.get("calls"),
            result.get("pending_days"),
        )

    def _push_brief(self, trade_date: date) -> None:
        """交易日收盘后把复盘简报推给用户。

        单独一步而不是塞进 `run()`：`run()` 也是「手动采集」和回补的入口，
        在那里推送会让每次手动补数都发一条消息。

        失败只记日志。推送依赖的是 Trae 注入的短效 Token（见 push_brief 模块说明），
        服务重启后会自动重试一次，所以这里不做别的兜底。
        """
        # 延迟导入：push_brief 要读 collect_daily 的指数口径，模块级导入会成环
        from app.jobs.push_brief import already_pushed, push

        if not self.settings.feishu_push_enabled:
            return
        if already_pushed(trade_date):
            logger.info("简报 %s 已推送过，跳过", trade_date)
            return
        try:
            result = push(trade_date, self.settings)
        except Exception:  # noqa: BLE001 - 推送失败绝不能影响调度
            logger.exception("推送 %s 简报失败", trade_date)
            return
        logger.info("简报 %s：%s %s", trade_date, result.get("status"), result.get("reason") or "")

    def _push_pattern(self, pattern: str, trade_date: date) -> None:
        """把某个形态的命中清单单独推一条（当日没有命中就不推）。

        与复盘简报**分成两条消息**：这条回答的是「今天买什么」，混在复盘里会被淹掉。
        每个形态**各有各的去重任务名**（见 `PUSH_PATTERNS`）—— 共用一条记录的话，
        哪天简报先发成功、这条就被顶掉了，而两者的发送条件本来就不一样。
        """
        # 延迟导入：与 `_push_brief` 同理，避免模块级循环依赖
        from app.jobs.push_brief import PUSH_PATTERNS, already_pushed, push_pattern_brief

        task = PUSH_PATTERNS[pattern][1]
        if not self.settings.feishu_push_enabled:
            return
        if already_pushed(trade_date, task):
            logger.info("%s %s 已推送过，跳过", pattern, trade_date)
            return
        try:
            result = push_pattern_brief(pattern, trade_date, self.settings)
        except Exception:  # noqa: BLE001 - 推送失败绝不能影响调度
            logger.exception("推送 %s 的 %s 失败", trade_date, pattern)
            return
        logger.info(
            "%s %s：%s %s",
            pattern,
            trade_date,
            result.get("status"),
            result.get("reason") or "",
        )

    def _scan_dde(self, trade_date: date) -> None:
        """全市场 DDE 扫描并推送（条件：5日DDE 由负转正，见 `scan_dde` 模块）。

        **每天十几次 iFinD 调用**（代码前缀 × 单个交易日）。配额紧张时先让它停 ——
        它是增强项，而指数 / 涨停 / 板块 / 情绪那条主线才是复盘的地基。让路阈值
        与形态日线同一档（80%），别等它把额度吃到影响次日的基础采集。
        """
        # 延迟导入：与 `_push_brief` 同理，避免模块级循环依赖
        from app.jobs.scan_dde import run as run_dde_scan
        from app.services.usage import QuotaLevel, level_label, quota_level

        level = quota_level(trade_date, self.settings)
        if level >= QuotaLevel.PAUSE_KLINE:
            logger.warning("DDE 扫描跳过：%s", level_label(level))
            return
        try:
            result = run_dde_scan(trade_date, self.settings)
        except Exception:  # noqa: BLE001 - 扫描失败绝不能影响调度
            logger.exception("DDE 扫描失败")
            return
        logger.info("DDE 扫描 %s：%s", trade_date, result)

    def _maybe_remind_calibration(self, trade_date: date) -> None:
        """每隔 `CALIBRATION_INTERVAL_DAYS` 天提醒一次配额对账。

        用「距上次提醒过了多少天」判断，而**不用 APScheduler 的间隔触发器**：
        后者的计时器在内存里，服务一重启就从头计时 —— 而这个服务每次部署都会重启，
        那样永远等不到 15 天。
        """
        from app.jobs.push_brief import (
            CALIBRATION_INTERVAL_DAYS,
            last_calibration_reminder,
            push_calibration_reminder,
        )

        if not self.settings.feishu_push_enabled:
            return
        last = last_calibration_reminder()
        if last is not None and (trade_date - last).days < CALIBRATION_INTERVAL_DAYS:
            return
        try:
            result = push_calibration_reminder(trade_date, self.settings)
        except Exception:  # noqa: BLE001 - 推送失败绝不能影响调度
            logger.exception("推送 %s 对账提醒失败", trade_date)
            return
        logger.info(
            "对账提醒 %s：%s %s",
            trade_date,
            result.get("status"),
            result.get("reason") or "",
        )

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
