"""涨停原因采集（**同花顺**口径）。

与 `collect_themes` 是两张表、两个来源，别混：

| 表 | 来源 | 回答的问题 |
| --- | --- | --- |
| `stock_concept` | 开盘红涨停天梯 | 这只票属于哪个**精选板块**（板块视角） |
| `limit_reason` | 同花顺涨停池 | 它今天**为什么**涨停（「房地产+城市更新+北京国资」） |

按日**整段替换**而不是增量 upsert：原因在盘后会被修（盘中给的常是初稿），而本表是
「某日快照」语义 —— 只补不删会留下已经过时的旧行。

零 iFinD 配额（走同花顺数据中心），所以配额紧张时也不用像板块那样让路。
"""

import logging
from datetime import date, timedelta

from sqlalchemy import delete, func, select

from app.config import Settings, get_settings
from app.db import session_scope
from app.models import LimitPool, LimitReason, TradeCalendar
from app.sources.ths_limit_up import ThsLimitUpSource

logger = logging.getLogger(__name__)

# `collect_range` 缺省回看多少个自然日（约 252 个交易日，与板块历史对齐）
DEFAULT_BACKFILL_DAYS = 380
PROGRESS_EVERY = 20


def _pool_up_count(trade_date: date) -> int:
    """当天站内涨停池（`pool_type="up"`）的家数。

    它是区分「这天真的没有涨停」与「同花顺没给」的**唯一**依据（2026-10-22 体检）。
    调用方拿 0 行时先看它：有票还 0 行 = 取数坏了；票也是 0 = 那天本来就没有。
    """
    with session_scope() as session:
        return (
            session.scalar(
                select(func.count())
                .select_from(LimitPool)
                .where(LimitPool.trade_date == trade_date, LimitPool.pool_type == "up")
            )
            or 0
        )


class ReasonCollector:
    """涨停原因采集器。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.source = ThsLimitUpSource(self.settings)

    def collect(self, trade_date: date) -> int:
        """抓当日涨停原因并落库，返回写入行数。

        ⚠️ **0 行不能记成功**（2026-10-22 体检）：A 股每个交易日都有涨停，
        同花顺却会因为「超出回溯范围 / 反爬 / 接口改了」返回空表（`ths_limit_up` 把
        这些都当正常空返回）。原来这里直接 `return 0`，`_step` 记 `ok rows=0`，
        `has_collected`（`reasons` 是 RUN_ALWAYS_TASKS 之一）就把这天当成采完了 ——
        涨停原因永久缺。所以在 `return 0` **之前**先用站内涨停池对账：有票还 0 行
        就抛错（`_step` 记 failed、下次启动重跑）。
        """
        rows = self.source.limit_reasons(trade_date)
        # 对账放在「0 行早退」之前 —— 放在后面的话，真正需要它的那一次永远执行不到。
        pool_count = _pool_up_count(trade_date)
        if not rows:
            if pool_count:
                raise RuntimeError(
                    f"{trade_date} 涨停原因 0 行，但站内涨停池有 {pool_count} 只 —— "
                    "同花顺没返回（超回溯范围 / 反爬 / 接口变了），不能记成「没有原因」"
                )
            # 站内涨停池也是 0：非交易日（周末 / 节假日）或真的一天没有涨停
            logger.warning("%s 涨停原因 0 行（涨停池 0 只，属正常空）", trade_date)
            return 0

        records = [
            {"trade_date": trade_date, "code": item["code"], "reason": item["reason"]}
            for item in rows
            if item["reason"]
        ]
        if len(records) < len(rows):
            logger.warning(
                "%s 有 %d 只票没给原因，已跳过（不写空行）",
                trade_date,
                len(rows) - len(records),
            )

        # 与站内涨停池对账。两者来源不同（一个同花顺、一个东财），差几只很正常
        # （ST / 新股 / 北交所的处理口径不同），差太多说明有一边出问题了。
        if pool_count and abs(len(records) - pool_count) > max(5, pool_count * 0.15):
            logger.warning(
                "%s 家数对不上：同花顺原因 %d 只 vs 站内涨停池 %d 只，两边都要看一眼",
                trade_date,
                len(records),
                pool_count,
            )

        with session_scope() as session:
            session.execute(
                delete(LimitReason).where(LimitReason.trade_date == trade_date)
            )
            for record in records:
                session.merge(LimitReason(**record))
        logger.info("%s 涨停原因落库 %d 行", trade_date, len(records))
        return len(records)

    def collect_range(
        self,
        end: date,
        *,
        start: date | None = None,
        days: int = DEFAULT_BACKFILL_DAYS,
    ) -> dict:
        """回补一段区间的每个交易日。

        成本与区间成正比（每天 1 次请求，零配额），所以穷举交易日历即可。
        每个交易日是整天替换，中断了直接重跑。

        ⚠️ 单日失败**不中断整轮**（2026-10-22 体检）：`collect` 现在会对「有涨停却
        0 行」抛错，逐日兜住才不会让一句坏响应把剩下几百天全停掉。失败天数单独计数。
        """
        since = start or (end - timedelta(days=days))
        with session_scope() as session:
            day_list = list(
                session.scalars(
                    select(TradeCalendar.trade_date)
                    .where(
                        TradeCalendar.trade_date >= since,
                        TradeCalendar.trade_date <= end,
                    )
                    .order_by(TradeCalendar.trade_date)
                )
            )
        if not day_list:
            logger.warning(
                "交易日历里 %s ~ %s 没有交易日，回补无从下手（先采一次日历）", since, end
            )
            return {"days": 0, "written": 0, "empty_days": 0, "failed_days": 0}

        written = 0
        empty_days = 0
        failed_days = 0
        for index, day in enumerate(day_list, 1):
            failed = False
            try:
                count = self.collect(day)
            except Exception as exc:  # noqa: BLE001 - 单日失败不拖垮整轮回补
                failed_days += 1
                failed = True
                logger.warning("涨停原因回补 %s 失败：%s", day, exc)
                count = 0
            written += count
            if count == 0 and not failed:
                empty_days += 1
            if index % PROGRESS_EVERY == 0:
                logger.info("涨停原因回补进度 %d/%d（最新 %s）", index, len(day_list), day)

        logger.info(
            "涨停原因回补完成：%d 个交易日，%d 行，%d 天空返回，%d 天失败",
            len(day_list),
            written,
            empty_days,
            failed_days,
        )
        return {
            "days": len(day_list),
            "written": written,
            "empty_days": empty_days,
            "failed_days": failed_days,
        }
