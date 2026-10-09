"""定时采集任务。

交易日**收盘后**自动采集当日数据（时刻定义在 `config.collect_hour/collect_minute`，
链条起点，简报在采集跑完后紧接着发）。

⚠️ 时刻**不能贴着收盘取**：龙虎榜与机构席位（东财 datacenter）要等收盘后一段时间才发布，
而采集是按「当天」取的 —— 取不到就留下一个没人回头补的洞（实测 2026-09-24：
15:25 取当天龙虎榜空、17:05 有 42 行）。

2026-09-28 用户要求改成 **15:05**，代价就是接受那个洞（当天龙虎榜不入库），
换来一收盘就能看到简报与形态 —— 见 `config.collect_hour` 下那段完整记录。

另有三处针对「本机不常开」的处理：
- **启动补采**：错过采集时刻才开机时，启动后在后台补跑一次
- **错过触发的宽限**：misfire_grace_time 一小时内仍会补跑，且 coalesce 保证只跑一次
- **尾部链路每交易日只跑一次**：补采与定时采集共用同一个入口，见 `TAIL_TASK` ——
  没有这道闸的话，**每次重启都会把尾部重跑一遍**（部署日一天能重启十几次）

不要用多 worker 启动本服务：每个 worker 会各自拉起一个调度器，
同一时刻会重复采集。默认单 worker 运行即符合预期。
"""

import logging
import threading
from datetime import date, datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select

from app.config import Settings, get_settings
from app.db import session_scope
from app.jobs.collect_daily import CollectionBusy, DailyCollector, collect_guard
from app.jobs.collect_sectors import SectorCollector
from app.models import CollectLog, TradeCalendar
from app.sources.ifind import IfindError

logger = logging.getLogger(__name__)

TIMEZONE = "Asia/Shanghai"
JOB_ID = "collect_daily"
# 收盘后那一趟（龙虎榜 / 机构席位 / 涨停题材 / 两融 / 北向成交）。为什么单独一个 job：
# 这几样在 15:05 还没发布，见 `app.jobs.collect_daily.DailyCollector.run_late` 的说明。
LATE_JOB_ID = "collect_late"
# 同一趟的**兜底重跑**（见 config.late_retry_hour）：这些来源当天什么时候更新不在我们
# 手上，问早了又**不报错、只给前一天的**，所以固定跑两趟，赌错的那天还有第二次机会。
LATE_RETRY_JOB_ID = "collect_late_retry"
# 当天板块成分股的预取（见 config.members_collect_hour）：开盘红要到晚上才发布当天
# 成分股，所以这一趟比「收盘后」那两趟都晚。
MEMBERS_JOB_ID = "prefetch_members"
# 它的兜底重跑（见 config.members_retry_hour）：22:00 撞上「还没发布」时再给一次机会。
# 正常日子这一趟几乎不花请求（已在库的板块全跳过）。
MEMBERS_RETRY_JOB_ID = "prefetch_members_retry"


def _is_trade_day(day: date) -> bool:
    """该日是否交易日（直接查日历表）。

    这里不借 `DailyCollector.is_trade_day`：成分股预取走的是开盘红、**与 iFinD 无关**，
    而构造 `DailyCollector` 会因为 iFinD 没配好而抛 `IfindError`。
    """
    with session_scope() as session:
        return bool(
            session.scalar(
                select(func.count())
                .select_from(TradeCalendar)
                .where(TradeCalendar.trade_date == day)
            )
        )


def _previous_trade_day(day: date) -> date | None:
    """`day` 之前最近的一个交易日（不含 `day` 本身）。日历为空时返回 None。

    给「成分股当日还没发布」的回落用（见 `_run_members`）。
    """
    with session_scope() as session:
        return session.scalar(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date < day)
            .order_by(TradeCalendar.trade_date.desc())
            .limit(1)
        )


# 尾部链路的去重标记（写在 `collect_log`），每个交易日一条。
#
# 尾部指的是采集之后那一串：日线增量 → 历史回补 → 形态扫描 → 简报/形态推送 →
# DDE 扫描 → 板块资金流。采集本身有「今天有没有数据」这道守卫（`has_collected`）
# 挡着，所以重复触发不会重复花钱；**尾部没有**，于是它跟着 `_run_daily` 的每一次
# 调用重跑一遍。
#
# 而 `_run_daily` 的调用方有两个：17:30 的 cron，以及**每次服务启动**的
# `_catch_up`（过了采集时刻就补跑）。后者在部署日会被反复触发 —— 实测 2026-09-21
# 那天晚上的 19:39~23:50 跑了 18 轮日线回补（每轮 4 天 / 56 次调用，合计约 950 次
# iFinD 调用），09-22 又跑了 5 轮（约 280 次），两天占了那个订阅周期真实消耗的
# 三分之一。而这本是个「每天往前啃一小段历史」的慢活（见 `kline_backfill_days`），
# 一天跑 18 遍只会把配额提前烧掉，并不会让历史补得更完整。
#
# DDE 扫描同理：09-22 那天连扫 5 轮（每轮 19 次），因为前面几轮都「无命中、未推送」，
# `already_pushed` 那道去重对没推送成功的情况不起作用。
TAIL_TASK = "daily_tail"


def tail_done(day: date) -> bool:
    """该日的尾部链路是否已经完整跑过一遍。"""
    with session_scope() as session:
        return bool(
            session.scalar(
                select(func.count())
                .select_from(CollectLog)
                .where(
                    CollectLog.trade_date == day,
                    CollectLog.task == TAIL_TASK,
                    CollectLog.status == "ok",
                )
            )
        )


def _mark_tail(day: date, reason: str) -> None:
    """记下「这天的尾部跑完了」。

    记录本身失败只告警：它的唯一作用是抑制重复触发，写不进去最多退回旧行为
    （重启再跑一遍），不该把一次已经跑完的采集标成失败。
    """
    try:
        with session_scope() as session:
            session.add(
                CollectLog(
                    trade_date=day,
                    task=TAIL_TASK,
                    status="ok",
                    message=f"尾部链路完成（{reason}）",
                )
            )
    except Exception:  # noqa: BLE001 - 见 docstring：记录失败不能影响采集
        logger.warning("写尾部链路记录失败，下次重启可能重跑一遍", exc_info=True)


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
        # 收盘后那一趟：15:05 采不到的那几类（龙虎榜 / 机构席位 / 涨停题材 / 两融 / 北向）
        #
        # **挂成两个 job、跑同一份代码**（2026-09-30）：这些来源「当天什么时候更新」不在
        # 我们手上，只赌一个时刻，赌错的那天就整天空着（问早了不报错、只给前一天的，
        # 见设计文档 §8.78）。17:30 尽早拿一次、19:30 兜底再拿一次，各步都是幂等重跑，
        # 成本见 config.late_retry_hour。
        #
        # ⚠️ 为什么不是「一个 job 带两个 trigger」：apscheduler 3.x 的 `add_job` **不接受
        # trigger 列表**（3.11.3 实测直接 TypeError: Expected a trigger instance or string,
        # got list instead）。两个 id 各挂一个，`status()` 里取两者较早的 next_run。
        for job_id, label, hour, minute in (
            (
                LATE_JOB_ID,
                "收盘后数据补采",
                self.settings.late_collect_hour,
                self.settings.late_collect_minute,
            ),
            (
                LATE_RETRY_JOB_ID,
                "收盘后数据补采（兜底）",
                self.settings.late_retry_hour,
                self.settings.late_retry_minute,
            ),
        ):
            scheduler.add_job(
                self._run_late,
                CronTrigger(
                    day_of_week="mon-fri", hour=hour, minute=minute, timezone=TIMEZONE
                ),
                kwargs={"reason": label},
                id=job_id,
                name=label,
                replace_existing=True,
                misfire_grace_time=3600,
                coalesce=True,
            )
        # 当天板块成分股预取（见 config.members_collect_hour）：开盘红要到晚上才发布
        # 当天成分股，所以它比上面那两趟都晚 —— 不是为了「早」，而是为了「发布之后立刻
        # 取一遍」，让之后打开任何板块都是读库。
        #
        # 同样给两个时刻（22:00 + 22:30）：开盘红「当晚几点发布」不确定，22:00 撞上还没
        # 发布时那一轮会整轮中止，22:30 再给一次机会。第二次**几乎不花请求** ——
        # 已经在库的板块全跳过，正常日子只会打一条「都已在库，无需重取」。
        for job_id, label, hour, minute in (
            (
                MEMBERS_JOB_ID,
                "板块成分股预取",
                self.settings.members_collect_hour,
                self.settings.members_collect_minute,
            ),
            (
                MEMBERS_RETRY_JOB_ID,
                "板块成分股预取（兜底）",
                self.settings.members_retry_hour,
                self.settings.members_retry_minute,
            ),
        ):
            scheduler.add_job(
                self._run_members,
                CronTrigger(
                    day_of_week="mon-fri", hour=hour, minute=minute, timezone=TIMEZONE
                ),
                kwargs={"reason": label},
                id=job_id,
                name=label,
                replace_existing=True,
                misfire_grace_time=3600,
                coalesce=True,
            )
        scheduler.start()
        self._scheduler = scheduler
        logger.info(
            "定时采集已启动：交易日 %02d:%02d（收盘后补采 %02d:%02d / 兜底 %02d:%02d，"
            "成分股预取 %02d:%02d / 兜底 %02d:%02d）",
            self.settings.collect_hour,
            self.settings.collect_minute,
            self.settings.late_collect_hour,
            self.settings.late_collect_minute,
            self.settings.late_retry_hour,
            self.settings.late_retry_minute,
            self.settings.members_collect_hour,
            self.settings.members_collect_minute,
            self.settings.members_retry_hour,
            self.settings.members_retry_minute,
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
        # 收盘后那两个 job（正点 + 兜底）取**较早**的下次运行：页面只显示一个「下次运行」，
        # 写较早的那个才不会被误读成「17:30 不跑了」
        late_nexts = [
            getattr(self._scheduler.get_job(job_id), "next_run_time", None)
            for job_id in (LATE_JOB_ID, LATE_RETRY_JOB_ID)
            if self._scheduler
        ]
        late_nexts = [value for value in late_nexts if value is not None]
        late_next = min(late_nexts) if late_nexts else None
        return {
            "enabled": self.settings.scheduler_enabled,
            "running": bool(self._scheduler and self._scheduler.running),
            "collect_time": (
                f"{self.settings.collect_hour:02d}:{self.settings.collect_minute:02d}"
            ),
            # 收盘后那一趟（15:05 采不到的那几类）—— 页面要显示它，否则用户看到
            # 「龙虎榜 0 条」会以为是坏了
            "late_collect_time": (
                f"{self.settings.late_collect_hour:02d}:{self.settings.late_collect_minute:02d}"
            ),
            # 同一趟的兜底时刻（见 config.late_retry_hour）。`late_next_run_time` 是这两个
            # 时刻里**较早**的那个 —— 两个触发挂在同一个 job 上，apscheduler 自己取最早
            "late_retry_time": (
                f"{self.settings.late_retry_hour:02d}:{self.settings.late_retry_minute:02d}"
            ),
            # 当天板块成分股的预取时刻（见 config.members_collect_hour）
            "members_collect_time": (
                f"{self.settings.members_collect_hour:02d}:"
                f"{self.settings.members_collect_minute:02d}"
            ),
            # 它的兜底时刻（见 config.members_retry_hour）：22:00 撞上「还没发布」时再试一次。
            # 正常日子那一趟几乎不花请求（已在库的板块全跳过）
            "members_retry_time": (
                f"{self.settings.members_retry_hour:02d}:"
                f"{self.settings.members_retry_minute:02d}"
            ),
            "late_next_run_time": late_next.isoformat() if late_next else None,
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
            # 交易日历为空时 `is_trade_day` 保守返回 False，于是**全新部署的库**
            # 在这里直接 return —— 日历永远采不到，整条定时链一次都跑不起来
            # （2026-09-27 修；日志还会显示成「今天不是交易日」，很有误导性）。
            # 先把「空日历」与「今天确实休市」分开：空日历就补一次再判断。
            if collector.calendar_empty():
                logger.info("%s：交易日历为空，先补一次日历再判断", reason)
                try:
                    collector.collect_calendar()
                except Exception:  # noqa: BLE001 - 补不到就照旧跳过，别让调度崩掉
                    logger.exception("%s：补交易日历失败", reason)
            if not collector.is_trade_day(today):
                # 占位符是**两个**（跳过的原因 + 哪一天），少传一个会让 logging 抛
                # TypeError —— 它被 logging 自己接住，只往 stderr 打一段栈，日志里
                # 反而看不到「不是交易日」这句（2026-09-27 部署当天实测踩到）
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

        # 尾部链路每个交易日只跑一次，见 TAIL_TASK。
        # 闸门放在这里而不是 `_catch_up` 里：cron 与启动补采走的是同一个入口，
        # 放在入口内两条路才受同一份记录约束（重启后的补采是重复触发的主要来源）。
        if tail_done(today):
            logger.info("%s：%s 的尾部链路已跑过，跳过", reason, today)
            return

        # 整条尾巴包进采集锁（2026-09-27 修）：`tail_done` 与 `_mark_tail` 是
        # 「先查后写」，两个 `_run_daily` 并发时（部署日重启、旧进程的尾巴还没跑完）
        # 会**都看到「没跑过」而双跑** —— DDE 全市场扫描一轮十几次调用、板块资金流
        # 三百七十多次请求，纯属白烧。（这道锁以前还要挡并发的
        # `POST /api/admin/collect`，那个接口 2026-09-28 已删 —— 现在并发的来源只有
        # cron 与启动补采两条路，都走这个入口。）
        # 拿不到锁就跳过且**不写标记**（尾巴确实没跑完），下次触发还会再试。
        try:
            with collect_guard(f"{reason} 尾部"):
                self._run_tail(today, reason)
        except CollectionBusy as exc:
            logger.warning("%s：尾部链路跳过（%s）", reason, exc)

    def _run_late(self, reason: str = "收盘后补采") -> None:
        """17:30 那一趟：只补「收盘后才发布」的数据（见 `DailyCollector.run_late`）。

        **没有「今天采过没有」那道守卫**（与 `_run_daily` 不同）：这几类数据很小、
        写库是幂等的（同一天覆盖写），重复触发最多多花 1 次 akshare + 2 次 EDB，
        不值得再为它加一套去重标记。
        """
        today = date.today()
        try:
            collector = DailyCollector(self.settings)
        except IfindError as exc:
            logger.warning("%s 跳过：%s", reason, exc)
            return

        if not collector.is_trade_day(today):
            logger.info("%s 跳过：%s 不是交易日", reason, today)
            return

        try:
            result = collector.run_late(today)
        except Exception:  # noqa: BLE001 - 定时任务绝不能因异常中断调度
            logger.exception("%s 失败", reason)
            return

        failed = [
            name
            for name, step in result.get("steps", {}).items()
            if step.get("status") != "ok"
        ]
        if failed:
            logger.warning("%s 完成但部分步骤失败：%s", reason, failed)
        else:
            logger.info("%s 完成：%s", reason, result.get("trade_date"))

    def _run_members(self, reason: str = "板块成分股预取") -> None:
        """22:00 那一趟（+ 22:30 兜底）：把当天的**板块成分股**一次性预取进库。

        为什么单独一趟：开盘红要到**晚上**才发布当天成分股（实测 21:20 还是
        `errcode=1020`、21:55 才有），所以白天点开任何板块都是「0 只 + 一句说明」，
        而发布之后每个板块又要各等几秒现取。这一趟跑完，之后都是读库。
        零 iFinD 配额（开盘红），已在库的板块自动跳过 —— 所以重复触发很便宜。

        **不需要「今天采过没有」那道守卫**：`collect_all_members(only_missing=True)`
        本身就是幂等跳过；也正因为如此，22:30 那趟兜底在正常日子只会打一条
        「都已在库，无需重取」，不必做成条件调度。
        """
        today = date.today()
        if not _is_trade_day(today):
            logger.info("%s 跳过：%s 不是交易日", reason, today)
            return

        try:
            result = SectorCollector(self.settings).collect_all_members(today)
        except Exception:  # noqa: BLE001 - 定时任务绝不能因异常中断调度
            logger.exception("%s 失败", reason)
            return

        if result.get("aborted"):
            logger.warning(
                "%s 中止：开盘红 %s 的成分股还没发布（连着 %d 个板块取不到）",
                reason,
                today,
                result.get("failed", 0),
            )
            # 当日取不到就**改补「最近可用的一天」**（2026-10-09 修）。
            # 页面本来就支持「拿最近一份名单 + 当日行情」回落（`api/sector._fallback_members`），
            # 缺的只是那份名单从没被缓存 —— 于是只有**被点开过**的板块才有行，其余永久空白，
            # 面板只能显示一句说明（用户报的「板块成分股还是不显示」就是这个）。
            # 为什么不能只等「当日」：实测它发布得很晚 —— 2026-10-08 连 22:42 都还是
            # `errcode=1020`，而这一趟固定 22:00 / 22:30 跑，等于每晚必空。
            # 补最近可用日之后，每个板块都有名单可回落，页面一打开就能看到票。
            prev = _previous_trade_day(today)
            if prev is None:
                return
            try:
                fallback = SectorCollector(self.settings).collect_all_members(prev)
            except Exception:  # noqa: BLE001 - 回落失败也不能中断调度
                logger.exception("%s 回落补 %s 失败", reason, prev)
                return
            logger.info(
                "%s 回落：%s 还没发布，改补最近可用日 %s —— 共 %d 个板块，写入 %d 行，失败 %d 个",
                reason,
                today,
                prev,
                fallback.get("boards", 0),
                fallback.get("written", 0),
                fallback.get("failed", 0),
            )
            return
        logger.info(
            "%s 完成：%s 共 %d 个板块，写入 %d 行，失败 %d 个",
            reason,
            result.get("trade_date"),
            result.get("boards", 0),
            result.get("written", 0),
            result.get("failed", 0),
        )

    def _run_tail(self, trade_date: date, reason: str) -> None:
        """采集之后那一串：日线 → 回补 → 形态 → 简报/推送 → DDE → 板块资金流。

        由 `_run_daily` 在**采集锁内**调用（见那里的说明）。
        """
        today = trade_date
        # 推送与采集解耦：上面「已有数据」分支会跳过采集，但简报该发还是要发 ——
        # 否则那天采过一遍之后就不会再有简报了。
        self._collect_kline(today)
        # 历史回补排在**形态扫描之前**：它只花固定几十次调用，
        # 而形态扫描是全市场日线的下游，先让历史落库再算，两者不抢
        self._backfill_kline(today)
        self._scan_patterns(today)
        # 命中前 N 只的**日线**补齐：形态页那张 K 线读的就是 `stock_daily`，某只的最后
        # 一根早于最近交易日，图上最新几天就是断的（2026-10-09 用户要求）。
        # 排在形态扫描之后 —— 要先有命中才谈得上「前 50 只」。走免费源为主、
        # 常态零 iFinD 配额，所以**不像 DDE 那一步需要按配额让路**。
        self._backfill_hit_kline(today)
        self._push_brief(today)
        # 单独推送的形态：加新形态就在 `PUSH_PATTERNS` 里登记，然后在这里补一行
        self._push_pattern("limit_surge_flat", today)
        # DDE 扫描排最后：它是这条链上**唯一要花十几二十次调用**的一步（前缀 × 单日），
        # 前面几步里有零配额的，先跑完再说
        self._scan_dde(today)
        # 命中前 N 只的 DDE 补齐：排在 DDE 扫描之后 —— 那一步已经把「当天」写进全市场，
        # 这里只补这几只票**剩下的历史**（fill_only 不会覆盖已有值）
        self._backfill_hit_dde(today)
        # 板块资金流排在**最末尾**：它要用上面 DDE 那一步的逐股净流入，自己还要走
        # 370 多次开盘啦请求（约 2~3 分钟），零 iFinD 配额所以不受配额让路影响 ——
        # 依赖缺失时它自己会跳过（不写一堆 0，见 `collect_board_flow` 的守卫）
        self._collect_board_flow(today)
        self._maybe_remind_calibration(today)
        # 整条尾巴走完才落记录。中途被重启打断（部署）的话记录不写，
        # 下一次启动会重跑一遍 —— 宁可多跑一次，也不要把没跑完的当成跑过了
        _mark_tail(today, reason)

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

    def _backfill_hit_kline(self, trade_date: date) -> None:
        """把当日命中评分前 N 只（`Settings.kline_hit_top_n`，默认 50）的**日线**补到最近交易日。

        与 `_backfill_hit_dde` 同一口径取票（`collect_dde.top_hit_codes`，就是页面上
        「评分前 50」那批）。为什么要有这一步：形态页那张 K 线读的就是 `stock_daily`，
        某只的最后一根早于最近交易日，图上最新几天就是断的。

        **不需要让路阈值**（与 DDE 那一步不同）：补法走东财 → 腾讯（都免费），iFinD 只
        在两级都不可用时兜底 —— 正常日子 **0 次 iFinD 调用**，不占用基础采集的额度。
        已经到最近交易日的票不发请求（`backfill_top_kline` 先查最后一根），重启重跑不重复花钱。
        失败只记日志（这是增强项）。
        """
        from app.jobs.scan_patterns import backfill_top_kline

        try:
            result = backfill_top_kline(trade_date, limit=self.settings.kline_hit_top_n)
        except Exception:  # noqa: BLE001 - 日线补齐是增强，不该影响调度
            logger.exception("命中日线补齐失败")
            return
        logger.info(
            "命中日线补齐 %s：命中 %s 只 / 需补 %s 只 → 补 %s 只"
            "（东财 %s / 腾讯 %s / iFinD %s，失败 %s），目标日 %s",
            trade_date,
            result.get("codes"),
            result.get("pending"),
            result.get("synced"),
            result.get("via_eastmoney"),
            result.get("via_tencent"),
            result.get("via_ifind"),
            result.get("failed"),
            result.get("want"),
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
        一次拉完就占掉当前口径（9000，2026-09-28）的 78% —— 直接顶到 80% 的让路线、
        把当天的采集停掉。而历史恰恰是「越早的越补不回来」—— 只能拆成每天一小段，
        跨几个配额周期慢慢铺满。补完之后
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

    def _backfill_hit_dde(self, trade_date: date) -> None:
        """把当日形态命中里**评分最高的 N 只**的 DDE 补全（`Settings.dde_hit_top_n`）。

        为什么要这一步：个股页 DDE 那一栏要的是**这几只**的历史，而 `_scan_dde` 的全市场
        扫描与请求日期无关、每天只写当天一行。补的票与页面「命中列表」同一口径
        （按股票归并取最高分），否则两边对不上。

        成本 = N 次 iFinD 调用/交易日（默认 50 ≈ 1100 次/月，约占一个周期额度的 16%），
        所以跟着 `_scan_dde` 的让路阈值走：配额紧张时先停它，别去挤基础采集。
        已经补够的票不发请求（见 `backfill_top_hits`），重启重跑不会重复花钱。
        """
        from app.jobs.collect_dde import backfill_top_hits
        from app.services.usage import QuotaLevel, level_label, quota_level

        level = quota_level(trade_date, self.settings)
        if level >= QuotaLevel.PAUSE_KLINE:
            logger.warning("命中 DDE 补齐跳过：%s", level_label(level))
            return
        try:
            result = backfill_top_hits(
                trade_date,
                days=self.settings.dde_hit_days,
                limit=self.settings.dde_hit_top_n,
            )
        except Exception:  # noqa: BLE001 - 这是增强项，不该影响调度
            logger.exception("命中 DDE 补齐失败")
            return
        logger.info(
            "命中 DDE 补齐 %s：命中 %s 只 / 需补 %s 只 → 写 %s 行（%s 次调用，失败 %s），窗口 %s",
            trade_date,
            result.get("codes"),
            result.get("pending"),
            result.get("written"),
            result.get("calls"),
            result.get("failed"),
            result.get("window"),
        )

    def _collect_board_flow(self, trade_date: date) -> None:
        """板块资金流：开盘啦成分股 × 逐股净流入（见 `collect_board_flow` 模块）。

        零 iFinD 配额，但要 2~3 分钟、且**依赖 `_scan_dde` 的结果** —— 依赖不在时
        它自己会跳过并记日志（不写一堆 0）。失败只记日志，不影响别的步骤。
        """
        from app.jobs.collect_board_flow import aggregate

        try:
            result = aggregate(trade_date, self.settings)
        except Exception:  # noqa: BLE001 - 板块资金流是增强，不该影响调度
            logger.exception("板块资金流聚合失败")
            return
        logger.info(
            "板块资金流 %s：%s 个板块 → 写 %s 行（名单 %s，逐股覆盖 %s 只，用时 %ss）",
            trade_date,
            result.get("boards"),
            result.get("written"),
            result.get("member_date"),
            result.get("stocks"),
            result.get("cost_seconds"),
        )

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

        # 收盘后那一趟也补：服务一直开着时调度器会自己触发，但「17:30 之后才起来」
        # （部署、重启）就会错过 —— 那天的龙虎榜就再没人去采了（这正是要修的那个坑）。
        # 上面那个早返回不用担心：`now < 15:05` 时必然也 `< 17:30`。
        if (now.hour, now.minute) >= (
            self.settings.late_collect_hour,
            self.settings.late_collect_minute,
        ):
            threading.Thread(
                target=self._run_late,
                kwargs={"reason": "启动补采（收盘后）"},
                name="collect-catchup-late",
                daemon=True,
            ).start()

        # 成分股预取也补：22:00 那次常常正好撞上部署/重启（本机更是常年关机）。
        # **重复触发不贵**：`collect_all_members(only_missing=True)` 会把已在库的板块
        # 全部跳过，真跑完过的一轮只会打一条「都已在库」的日志。
        if (now.hour, now.minute) >= (
            self.settings.members_collect_hour,
            self.settings.members_collect_minute,
        ):
            threading.Thread(
                target=self._run_members,
                kwargs={"reason": "启动补采（成分股）"},
                name="collect-catchup-members",
                daemon=True,
            ).start()


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
