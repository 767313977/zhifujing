"""收盘后数据采集。

触发时机：交易日收盘后（`config.collect_hour/collect_minute`，2026-09-28 起是 15:05）。
⚠️ 15:05 时龙虎榜与机构席位还没发布 —— 那两项当天会空着，见 config 注释。
**没有手动入口**：`POST /api/admin/collect` 与页面上的按钮 2026-09-28 都删了
（点一下就白跑约 50 次 iFinD 调用）；要强制重采就重启服务，由「启动补采」补跑。

设计要点：
- 每步独立事务、独立日志，**单步失败不阻塞后续步骤**
- 数据源分工：iFinD 出指数行情；akshare 出涨停三池、龙虎榜、交易日历、市场活跃度
  （后四类 iFinD 完全没有）
- 情绪指标依赖涨停池与指数，故排在最后
"""

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta

from sqlalchemy import func, select

from app.config import Settings, get_settings
from app.db import session_scope, upsert, upsert_fill
from app.models import (
    CollectLog,
    IndexDaily,
    Lhb,
    LimitPool,
    MarketSentiment,
    StockBasic,
    StockDaily,
    TradeCalendar,
    Watchlist,
)
from app.jobs import collect_funds
from app.services.sentiment import build_sentiment
from app.services.usage import QuotaLevel, level_label, quota_level
from app.sources.akshare_source import AkshareSource
from app.sources.ifind import IfindClient, IfindError, from_ths_symbol, to_ths_symbol
from app.sources.markdown_table import pick_float, pick_int, pick_text, to_float, to_int

logger = logging.getLogger(__name__)

# 复盘用核心指数。iFinD 对无法识别的代码会**静默丢弃**，采集后必须校验返回行数。
# 回补时要按中文名提问（自然语言接口对简称更稳），落库仍用带后缀的代码。
INDEX_NAMES: dict[str, str] = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000688.SH": "科创50",
    "000852.SH": "中证1000",
    "899050.BJ": "北证50",
}
INDEX_SYMBOLS = list(INDEX_NAMES)
INDEX_INDICATORS = [
    "最新价",
    "涨跌幅",
    "成交额",
    "上涨家数",
    "下跌家数",
    "涨停家数",
    "跌停家数",
]
# iFinD 对同一指数可能返回不同交易所的代码：中证1000 既有 000852.SH 也有
# 399852.SZ（实测两者收盘价与涨跌幅完全一致），且同一次回补的不同日期区间
# 还可能返回不同的那个。不归一会造成同一指数出现两套代码的重复序列。
INDEX_CODE_ALIASES = {
    "399852.SZ": "000852.SH",
}
# 沪深两市成交额 = 上证指数 + 深证成指
AMOUNT_SYMBOLS = ("000001.SH", "399001.SZ")

# index_data 单次请求的日期跨度上限。
#
# ⚠️ **这个接口每次最多返回 100 行**（含周末这类非交易日），所以区间跨度超过
# 约 100 个自然日就必然被抽样截断 —— 而且**看不出来**：返回的行数正好是 100，
# 日期也连续，只有跟交易日历对一遍才知道少了天。实测：
#   119 个自然日 → 正好 100 行（2025-09-08 ~ 2026-01-05 的 41 个交易日就这么丢的）
#   60 个自然日  → 61 行，完整
# 取 60 留足余量：一个月多花几次调用，换「不静默丢数据」。
INDEX_BACKFILL_CHUNK_DAYS = 60

# 三池的数据源都只保留最近 15 个**交易日**。
# 实测（2026-09-17 往回数）：第 15 个交易日 2026-08-28 仍有数据，
# 再往前一天 2026-08-27 三个池全部返回空表。
#
# 这个坑很隐蔽：东财只对跌停/炸板做了 30「自然日」的校验（超出直接抛错），
# 而**涨停池不做任何校验**，超期只是返回空表 —— 空表与「当天真的 0 家涨停」
# 在代码里完全无法区分。所以必须自己按交易日判断窗口，
# 否则会把大量没有数据的日期记成 0 家涨停，让情绪曲线撒谎。
POOL_HISTORY_TRADING_DAYS = 15
POOL_TYPES = ("up", "down", "broken")
# 龙虎榜区间批量取的分片跨度（接口 pageSize=5000，约 1 个月不至于超页）
LHB_BACKFILL_CHUNK_DAYS = 30

# 个股日线单次请求的跨度（日历日）。实测 90 天完整、200 天会被抽样截断成
# 100 行并丢掉 69 个交易日。留余量取 70，且每块后校验完整性 ——
# 比硬编码一个猜出来的阈值更稳。
STOCK_CHUNK_DAYS = 70
# 首次同步回看的交易日数
STOCK_FULL_DAYS = 250
# 每日同步只回看最近几天，够覆盖当日即可
STOCK_DAILY_DAYS = 10

# `run()` 写入的步骤任务名。`has_collected` 只认这些步骤的状态（见它的说明）——
# 别的任务（题材 / 板块资金流 / DDE 推送 / 简报推送…）失败不代表基础采集没完成。
# **不含 `calendar`**：那一步是 `_step(None, "calendar", ...)`，trade_date 落的是 NULL、
# 按天查不到它，而且日历不属于「某一天采没采」。
RUN_STEP_TASKS = (
    "index",
    "limit_pool",
    "reasons",
    "lhb",
    "sentiment",
    "etf",
    "lhb_institution",
    "sectors",
    "watchlist",
    "margin",
    "hsgt",
)

# 自选股同步的熔断阈值：连着这么多只都失败就中止本轮（见 `sync_watchlist`）。
# 单只 iFinD 超时最长约 3 分钟，不熔断的话几十只票能把采集锁占几个小时。
WATCHLIST_CONSECUTIVE_FAILURES = 3


class CollectionBusy(RuntimeError):
    """已有采集任务在运行。"""


# 采集互斥锁：定时任务、手动采集、以及用户连点按钮都可能同时触发。
# 每个 DailyCollector 实例各有自己的令牌桶，两个采集器并发跑等于实际请求
# 速率翻倍，会直接触发 iFinD 429，所以整个采集过程必须串行。
_collect_lock = threading.Lock()

# 采集锁持有超过这么久就打一条 warning（含当前步骤名）—— 用来发现「卡在某个没有
# 超时的请求上」。akshare 那类内部不带 timeout 的调用正是主要目标（见
# `sources/akshare_source` 的限时包裹）：连接半开时它会一直等，把锁占死，
# 之后每一趟采集都 `CollectionBusy`，直到手动重启。
LOCK_HOLD_WARN_SECONDS = 600.0

# 当前正在执行的采集步骤名（`_step` 进/出时更新），供上面的告警读。
# 只覆盖 `DailyCollector` 的步骤；调度器尾部链路的步骤不经过这里，读到的会是
# 最后一个采集步骤或 None，够定位「卡在哪一步」就行。
_current_step: str | None = None


@contextmanager
def collect_guard(who: str = "采集") -> Iterator[None]:
    """采集互斥。

    拿不到锁说明已有任务在跑，**直接抛错而不是排队等待** ——
    排队会让调用方以为卡住了，不如明确告诉他稍后再试。
    """
    if not _collect_lock.acquire(blocking=False):
        raise CollectionBusy("已有采集任务在运行，请等它结束再试")
    logger.info("%s 取得采集锁", who)
    started = time.monotonic()

    def _watchdog() -> None:
        # 计时到点仍在跑就打一条；正常结束会在 finally 里 cancel 掉
        logger.warning(
            "%s 已持有采集锁超过 %d 秒（当前步骤：%s），可能卡在某个没有超时的请求上",
            who,
            int(LOCK_HOLD_WARN_SECONDS),
            _current_step or "未知（不在 run() 的步骤里）",
        )

    watchdog = threading.Timer(LOCK_HOLD_WARN_SECONDS, _watchdog)
    watchdog.daemon = True
    watchdog.start()
    try:
        yield
    finally:
        watchdog.cancel()
        _collect_lock.release()
        logger.info("%s 释放采集锁（持有 %.1fs）", who, time.monotonic() - started)


def _require_today(trade_date: date, what: str) -> None:
    """iFinD 高频行情接口只支持当日盘中/收盘快照，**不支持历史查询**。

    若拿它给历史日期取数，会把今天的数据写到历史日期上。这种静默的数据污染
    比直接失败危险得多，所以硬性拦截。
    """
    if trade_date != date.today():
        raise ValueError(
            f"{what} 依赖 iFinD 高频行情接口（仅当日），"
            f"请求日期 {trade_date} 非今日；历史行情需改用 index_data 等日频接口"
        )


def _pool_cutoff(recent_trade_dates: list[date]) -> date | None:
    """三池能取到数据的最早交易日。

    `recent_trade_dates` 需按**降序**传入（最近的在前），取前 N 个里的最小值。
    """
    window = recent_trade_dates[:POOL_HISTORY_TRADING_DAYS]
    return min(window) if window else None


def _parse_ymd(value: object) -> date | None:
    """iFinD 日期是 20260917 这种紧凑写法。"""
    text = str(value or "").strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        # 8 位数字也可能是非法日期（`20260231`）。不接住的话，**一行脏数据就会中断
        # 整轮回补** —— 而这个函数在 `backfill_index` / `sync_stock` 里逐行调用、
        # 外面没有 try（2026-09-27 修）
        logger.warning("忽略非法日期：%s", value)
        return None


def _date_chunks(start: date, end: date, span_days: int) -> list[tuple[date, date]]:
    """把日期区间切成若干段，避免自然语言接口单次区间过大被截断。"""
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=span_days - 1), end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def _single_day_amount(row: dict) -> float | None:
    """取单日成交额。

    **不能对「成交」做模糊匹配**：iFinD 在未显式指定指标时会自动生成
    「N日成交额」，那是滚动累计值（实测约为单日的 5 倍），列名里带「N日」。
    这里显式排除，避免把累计值当成单日值入库。
    """
    for column, value in row.items():
        if "成交" in column and "N日" not in column:
            return to_float(value)
    return None


def _canonical_index_code(code: str) -> str:
    """把 iFinD 返回的指数代码归一成同一指数唯一代码。"""
    normalized = code.strip().upper()
    return INDEX_CODE_ALIASES.get(normalized, normalized)


def _index_nl_rows(records: list[dict], trade_dates: set[date]) -> list[dict]:
    """把 index_data 的自然语言结果转成 IndexDaily 行。

    三道过滤：
    1. iFinD 的区间查询会返回**非交易日**，其涨跌幅为空、收盘价为前值填充，
       必须按交易日历剔除，否则会污染曲线
    2. 指数代码归一（同一指数可能返回沪/深两套代码）
    3. 涨跌家数不回补 —— 同一指数同一天，index_data 与 index_highfreq_quotes
       给出的数值不同（实测上证 0917：991 vs 1062），混用会破坏序列一致性
    """
    rows: list[dict] = []
    for record in records:
        day = _parse_ymd(pick_text(record, "日期"))
        raw_code = pick_text(record, "证券代码")
        if day is None or not raw_code or day not in trade_dates:
            continue
        rows.append(
            {
                "trade_date": day,
                "code": _canonical_index_code(raw_code),
                "name": pick_text(record, "证券简称"),
                "close": pick_float(record, "收盘价"),
                "pct_chg": pick_float(record, "涨跌幅"),
                "amount": _single_day_amount(record),
                # 历史涨跌家数口径不一致，留空而非填错值
                "up_count": None,
                "down_count": None,
                "limit_up_count": None,
                "limit_down_count": None,
            }
        )
    return rows


def _base(trade_date: date, record: dict, pool_type: str) -> dict:
    """涨停/跌停/炸板三池的公共字段。"""
    return {
        "trade_date": trade_date,
        "code": str(record["代码"]).zfill(6),
        "pool_type": pool_type,
        "name": record.get("名称"),
        "pct_chg": record.get("涨跌幅"),
        "price": record.get("最新价"),
        "amount": record.get("成交额"),
        "float_mv": record.get("流通市值"),
        "total_mv": record.get("总市值"),
        "turnover": record.get("换手率"),
        "industry": record.get("所属行业"),
    }


def _limit_up_rows(trade_date: date, records: list[dict]) -> list[dict]:
    """涨停池：有首次/最后封板时间、炸板次数、连板数。"""
    return [
        _base(trade_date, r, "up")
        | {
            "seal_amount": r.get("封板资金"),
            "first_seal_time": r.get("首次封板时间"),
            "last_seal_time": r.get("最后封板时间"),
            "open_times": r.get("炸板次数"),
            "consecutive": r.get("连板数"),
        }
        for r in records
    ]


def _limit_down_rows(trade_date: date, records: list[dict]) -> list[dict]:
    """跌停池：字段名不同 —— 封单资金、连续跌停、开板次数，且没有首次封板时间。"""
    return [
        _base(trade_date, r, "down")
        | {
            "seal_amount": r.get("封单资金"),
            "last_seal_time": r.get("最后封板时间"),
            "open_times": r.get("开板次数"),
            "consecutive": r.get("连续跌停"),
        }
        for r in records
    ]


def _broken_rows(trade_date: date, records: list[dict]) -> list[dict]:
    """炸板池：有首次封板时间，但没有封板资金、最后封板时间和连板数。"""
    return [
        _base(trade_date, r, "broken")
        | {
            "first_seal_time": r.get("首次封板时间"),
            "open_times": r.get("炸板次数"),
        }
        for r in records
    ]


def _lhb_rows(trade_date: date, records: list[dict]) -> list[dict]:
    def _day(value: object) -> date:
        # 上榜日从 akshare 来是字符串（"2026-09-18"）或 pandas Timestamp，
        # 必须归一化成 date —— 否则 backfill_lhb 里「date 集合」与字符串比较
        # 恒不相等，整批行被静默丢弃，回补龙虎榜「看着成功、实际一行没写」。
        text = str(value or "").strip()
        if text:
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                pass
        return trade_date

    # 按主键 (trade_date, code, reason) 去重：接口偶尔会吐重复行，同一批里带重复
    # 主键在 SQLite 的批量写入上行为未定义（`upsert_many` 会报错）。后出现的覆盖先出现的。
    rows: dict[tuple, dict] = {}
    for r in records:
        day = _day(r.get("上榜日"))
        code = str(r["代码"]).zfill(6)
        reason = str(r.get("上榜原因") or "未知")
        rows[(day, code, reason)] = {
            "trade_date": day,
            "code": code,
            # 同一股票可能因多条上榜原因重复出现，所以 reason 进主键
            "reason": reason,
            "name": r.get("名称"),
            "close": r.get("收盘价"),
            "pct_chg": r.get("涨跌幅"),
            "net_buy": r.get("龙虎榜净买额"),
            "buy_amount": r.get("龙虎榜买入额"),
            "sell_amount": r.get("龙虎榜卖出额"),
            "interpretation": r.get("解读"),
        }
    return list(rows.values())


def _stock_rows(records: list[dict], trade_dates: set[date]) -> list[dict]:
    """把个股日频结果转成 StockDaily 行。

    iFinD 返回的是**日历日**（含周末），非交易日值为空，必须按交易日历过滤，
    否则 K 线上会多出一堆空点。
    """
    rows: list[dict] = []
    for record in records:
        day = _parse_ymd(pick_text(record, "日期"))
        code = pick_text(record, "证券代码")
        if day is None or not code or day not in trade_dates:
            continue
        rows.append(
            {
                "trade_date": day,
                "code": from_ths_symbol(code),
                "name": pick_text(record, "证券简称"),
                "open": pick_float(record, "开盘价"),
                "high": pick_float(record, "最高价"),
                "low": pick_float(record, "最低价"),
                "close": pick_float(record, "收盘价"),
                "volume": pick_float(record, "成交量"),
                "amount": pick_float(record, "成交额"),
                "pct_chg": pick_float(record, "涨跌幅"),
            }
        )
    return rows


class DailyCollector:
    """日线采集器。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.ifind = IfindClient(self.settings)
        self.ak = AkshareSource(self.settings)
        # 步骤自己写的一句说明（如自选股「失败 3 只」）。`_step` 在每个步骤开始时
        # 清零、结束后读走写进 `collect_log.message` —— 部分失败不该让整步记 failed，
        # 但也不能只躺在日志里。见 `sync_watchlist` 与 `_step`。
        self._step_note: str | None = None

    # ------------------------------------------------------------------ 交易日

    def _calendar_head(self) -> date | None:
        today = date.today()
        with session_scope() as session:
            return session.scalar(
                select(TradeCalendar.trade_date)
                .where(TradeCalendar.trade_date <= today)
                .order_by(TradeCalendar.trade_date.desc())
                .limit(1)
            )

    def _calendar_max(self) -> date | None:
        """日历表里**最大**的日期（不按今天过滤，可能是未来日期）。"""
        with session_scope() as session:
            return session.scalar(select(func.max(TradeCalendar.trade_date)))

    def calendar_needs_refresh(self) -> bool:
        """交易日历是否需要（重新）拉取。

        - 表为空：全新部署，必须补（同 `calendar_empty`）
        - 表里**最大日期早于今天**：跨年时上游还没发布次年日历，akshare 的日历
          又只覆盖到已知的那一年 —— 不刷新就会**永久停更**：所有任务都判成
          「今天不是交易日」而整条定时链一次都跑不起来，且无法自愈。
          改成「最大日期早于今天就刷新一次」，上游一发布次年日历就能自己补上。
        """
        latest = self._calendar_max()
        return latest is None or latest < date.today()

    def latest_trade_date(self) -> date:
        """取不晚于今天的最近交易日。

        注意：akshare 返回的日历包含未来日期（如 2026-12-31），必须先过滤。
        """
        found = self._calendar_head()
        if found is None:
            self.collect_calendar()
            found = self._calendar_head()
        if found is None:
            raise RuntimeError("无法确定最近交易日，交易日历为空")
        return found

    def is_trade_day(self, day: date) -> bool:
        """该日期是否交易日。定时任务靠它跳过周末与节假日。

        交易日历没数据时保守返回 False，避免在非交易日白跑一遍采集
        —— 但这会让**全新库**永远起不来，调用方要先 `calendar_empty()` 判断，
        见 `scheduler._run_daily`。
        """
        with session_scope() as session:
            count = session.scalar(
                select(func.count())
                .select_from(TradeCalendar)
                .where(TradeCalendar.trade_date == day)
            )
        return bool(count)

    def calendar_empty(self) -> bool:
        """交易日历表是否为空。全新部署第一次启动时就是这样。"""
        with session_scope() as session:
            count = session.scalar(
                select(func.count()).select_from(TradeCalendar)
            )
        return not count

    def has_collected(self, day: date) -> bool:
        """该日是否已完成一次采集（可以跳过重采）。

        判据是**两个条件同时成立**：
        1. 情绪表有当天的行 —— 首页「今日复盘」有数据可看；
        2. `run()` **各步骤**当天**最新**一条状态里没有 `failed`。

        ⚠️ **只看 `run()` 的步骤、且只看每步最新一条**（2026-10-10 修）：原来的判据
        是「当天**任意一条** `failed`」，于是**与基础采集无关的任务失败也会把整条
        `run()` 拖着重跑** —— 开盘红天梯 17:30 才更新（当天先记 `failed`）、简报推送
        失败、DDE 推送失败，都会写 `failed`；之后每次重启都会重跑 `run()`
        （约 50 次 iFinD 调用 + 自选每只 1 次），而**重跑根本修不好这些**。
        收窄到 `run()` 步骤的最新状态后，那些尾部 / 推送任务的失败不再触发重跑；
        而基础步骤真失败时仍会重跑（各步都是幂等 upsert，不会写坏数据）。

        为什么「只看情绪表」也不行（2026-09-27 修）：情绪排在链路**中部**，它后面
        还有 ETF、龙虎榜机构席位、题材、板块、自选股、两融、北向 —— 只看情绪的话，
        ETF 失败后这一天再也不会被重试，而 ETF 份额是「最新-交易日」快照，
        **漏了当天就永久补不回来**。
        """
        with session_scope() as session:
            if not session.scalar(
                select(func.count())
                .select_from(MarketSentiment)
                .where(MarketSentiment.trade_date == day)
            ):
                return False
            # 每个 `run()` 步骤取**当天最新**一条日志的状态
            latest_ids = (
                select(func.max(CollectLog.id))
                .where(
                    CollectLog.trade_date == day,
                    CollectLog.task.in_(RUN_STEP_TASKS),
                )
                .group_by(CollectLog.task)
            )
            statuses = session.scalars(
                select(CollectLog.status).where(CollectLog.id.in_(latest_ids))
            ).all()
        return not any(status == "failed" for status in statuses)

    # -------------------------------------------------------------------- 步骤

    def collect_calendar(self) -> int:
        dates = self.ak.trade_calendar()
        with session_scope() as session:
            return upsert(session, TradeCalendar, [{"trade_date": d} for d in dates])

    def collect_index(self, trade_date: date) -> int:
        _require_today(trade_date, "指数快照")
        records = self.ifind.index_quotes(INDEX_SYMBOLS, INDEX_INDICATORS)
        if not records:
            # 0 行是明确的取数失败（这个接口不会「正常返回 0 行」），记 failed 让上层
            # 重试；不能当 ok —— 否则索引全空、行情全空，页面却是「这天没数据」
            raise IfindError(f"指数快照返回 0 行（{trade_date}），疑似接口异常")
        if len(records) < len(INDEX_SYMBOLS):
            returned = {pick_text(r, "证券代码") for r in records}
            # iFinD 对不认识的代码静默丢弃，不校验就会以为采全了
            logger.warning(
                "指数仅返回 %d/%d，缺失: %s",
                len(records),
                len(INDEX_SYMBOLS),
                sorted(set(INDEX_SYMBOLS) - returned),
            )

        rows = []
        for record in records:
            code = pick_text(record, "证券代码")
            if not code:
                continue
            rows.append(
                {
                    "trade_date": trade_date,
                    # 与回补路径（_index_nl_rows）一致：归一成同一指数唯一代码。
                    # 漏归一化的话，实时接口返回别名代码（如中证1000 的 399852.SZ）
                    # 就会在库里出现同一指数两套代码的两条序列
                    "code": _canonical_index_code(code),
                    "name": pick_text(record, "证券简称"),
                    "close": pick_float(record, "最新价", "收盘价"),
                    "pct_chg": pick_float(record, "涨跌幅"),
                    "amount": pick_float(record, "成交额"),
                    "up_count": pick_int(record, "上涨家数"),
                    "down_count": pick_int(record, "下跌家数"),
                    "limit_up_count": pick_int(record, "涨停家数"),
                    "limit_down_count": pick_int(record, "跌停家数"),
                }
            )
        with session_scope() as session:
            return upsert(session, IndexDaily, rows)

    def _pool_window_cutoff(self) -> date | None:
        """三池数据源能取到数据的最早交易日。"""
        with session_scope() as session:
            recent = list(
                session.scalars(
                    select(TradeCalendar.trade_date)
                    .where(TradeCalendar.trade_date <= date.today())
                    .order_by(TradeCalendar.trade_date.desc())
                    .limit(POOL_HISTORY_TRADING_DAYS)
                )
            )
        return _pool_cutoff(recent)

    def collect_limit_pool(self, trade_date: date) -> int:
        """采集涨停 / 跌停 / 炸板三池。

        每个池单独记一条日志（`pool_up` / `pool_down` / `pool_broken`），
        因为情绪指标必须区分「该池当日真的 0 家」与「该池当日取不到数」：
        前者是 0，后者必须是 None。超出数据源窗口的池记为 `skipped`。

        抛错规则（上层 `_step` 据此把这一步记成 failed，从而当天会重试）：
        **涨停池（`up`）失败即抛** —— 涨停池是这条链的核心（`limit_pool` 这一步的
        状态被 `has_collected` 用来判「这天采全没有」，涨停家数 / 龙头 / 题材的入口
        都建立在它上面），取不到就等于这天没采全；而它返回空表又会被误读成「0 家涨停」，
        所以不能静默放过。跌停池 / 炸板池失败**不阻塞**（缺它们不至于让整个涨停复盘塌掉）。
        三池**全部真失败**时也抛（兜底）。三池**全部超期跳过（skipped）**不算失败 ——
        那天本来就取不到，重试也没用。
        """
        jobs = (
            ("up", "涨停池", self.ak.limit_up_pool, _limit_up_rows),
            ("down", "跌停池", self.ak.limit_down_pool, _limit_down_rows),
            ("broken", "炸板池", self.ak.broken_pool, _broken_rows),
        )
        cutoff = self._pool_window_cutoff()
        written = 0
        failures: list[str] = []
        failed_types: list[str] = []

        for pool_type, label, fetch, to_rows in jobs:
            task = f"pool_{pool_type}"
            started = time.monotonic()

            # 超期必须自己判断：涨停池超期不报错只返回空表，会被误读成 0 家
            if cutoff is not None and trade_date < cutoff:
                message = (
                    f"数据源只保留最近 {POOL_HISTORY_TRADING_DAYS} 个交易日"
                    f"（{cutoff} 起），该日期已超出窗口，本池无数据"
                )
                logger.info("跳过 %s %s：%s", label, trade_date, message)
                self._log(trade_date, task, "skipped", 0, message, 0.0)
                continue

            try:
                records = fetch(trade_date)
                if pool_type == "up" and not records:
                    # 涨停池返回空 ≠「当天 0 家涨停」—— A 股每个交易日必有涨停。
                    # 空表只可能是来源返回 data=null（还没发布 / 接口坏了）。
                    # 记 failed（而不是 ok-0）：「涨停数」才不会被写成 0，也不会
                    # 让 `has_collected` 误以为这天采全了、不再重试。
                    raise RuntimeError("涨停池返回空（疑似来源 data=null，不是 0 家涨停）")
                rows = to_rows(trade_date, records)
            except Exception as exc:  # noqa: BLE001 - 单池失败不阻塞其他池
                message = f"{type(exc).__name__}: {exc}"
                logger.warning("采集 %s %s 失败: %s", label, trade_date, exc)
                self._log(
                    trade_date, task, "failed", 0, message,
                    round(time.monotonic() - started, 2),
                )
                failures.append(f"{label}({message})")
                failed_types.append(pool_type)
                continue

            with session_scope() as session:
                count = upsert(session, LimitPool, rows)
            written += count
            self._log(
                trade_date, task, "ok", count, None,
                round(time.monotonic() - started, 2),
            )

        # 涨停池（`up`）失败即算这一步失败（当天会重试）；跌停 / 炸板失败可容忍，
        # 只有「三池全部真失败」才兜底抛。全部超期跳过（skipped）不算失败。
        if "up" in failed_types or len(failures) == len(jobs):
            raise RuntimeError(
                f"{trade_date} 涨停池采集失败（或三池全败）: {'; '.join(failures)}"
            )
        return written

    def collect_lhb(self, trade_date: date) -> int:
        rows = _lhb_rows(trade_date, self.ak.lhb(trade_date))
        with session_scope() as session:
            return upsert(session, Lhb, rows)

    def collect_reasons(self, trade_date: date) -> int:
        """涨停原因（**同花顺**口径，零 iFinD 配额）。

        与 `collect_themes` 是两条线：那个给「属于哪个开盘红精选板块」，这个给
        「为什么涨停」（「房地产+城市更新+北京国资」）。排在涨停池之后，为的是能跟
        涨停池的家数对账。
        """
        from app.jobs.collect_reasons import ReasonCollector

        return ReasonCollector(self.settings).collect(trade_date)

    def collect_themes(self, trade_date: date) -> int:
        """涨停股的板块归属（「题材 × 涨停」联动的桥）。

        来源是开盘红的涨停天梯，它自己就带「所属板块」，所以**不再依赖涨停池**
        的代码去逐只问 —— 但仍排在涨停池之后，为的是能跟涨停池的家数对账。
        延迟导入避免 collect_daily 与 collect_themes 循环引用。
        """
        from app.jobs.collect_themes import ThemeCollector

        return ThemeCollector(self.settings).collect(trade_date)

    def collect_sectors(self, trade_date: date) -> int:
        """板块行情（开盘红口径）。

        只有一步：开盘红的板块排行接口一次给全某个口径当天的所有板块，
        精选与行业各拉一次即可 —— 旧版「指数补历史 + 一览表覆盖当日 + iFinD
        给概念兜底」的三步顺序在这里没有对应物，因为没有第二个来源了。
        延迟导入避免 collect_daily 与 collect_sectors 循环引用。
        """
        from app.jobs.collect_sectors import SectorCollector

        return SectorCollector(self.settings).collect_day(trade_date)

    # ------------------------------------------------------------ 个股日线

    def sync_stock(self, code: str, days: int = STOCK_FULL_DAYS) -> int:
        """把某只个股的日线同步进 stock_daily。

        本站不做全市场落库（会触发东财频控），只有自选股与看过的个股会缓存到本地，
        所以这里的量级很小。

        分块请求并在每块后校验完整性：iFinD 的区间过大会**抽样丢弃**而不是
        砍掉末尾，漏掉的交易日会让 K 线出现静默空洞，必须主动报警。

        ⚠️ **只补缺的交易日**（2026-10-10 修）：原来每块都无条件重拉，一只票
        `days=250` 就是 6 次调用、`days=500` 是 11 次，**本地明明已有也照拉** ——
        会员点一次「同步」白烧几十次配额。现在先查库里已有哪些交易日，只对
        「还缺着的日子」所在的块发请求；一块都不缺就一次接口都不打。
        """
        code = str(code).strip().zfill(6)
        end = self.latest_trade_date()
        # 交易日 → 日历日按 1.5 倍粗算，多取一些无妨（交易日历会过滤掉多余的）
        start = end - timedelta(days=int(days * 1.5))
        trade_dates = set(self._trade_dates(start, end))
        if not trade_dates:
            return 0

        with session_scope() as session:
            have = set(
                session.scalars(
                    select(StockDaily.trade_date).where(
                        StockDaily.code == code,
                        StockDaily.trade_date >= start,
                        StockDaily.trade_date <= end,
                    )
                )
            )
        needed = trade_dates - have
        if not needed:
            # 全都有：一次接口都不打（这正是「每天采集顺带刷一次」能承受的前提）
            return 0

        symbol = to_ths_symbol(code)
        written = 0
        for chunk_start, chunk_end in _date_chunks(start, end, STOCK_CHUNK_DAYS):
            # 这一块里「缺着、且落在本块」的交易日；空则整块跳过，不发请求
            want = {d for d in needed if chunk_start <= d <= chunk_end}
            if not want:
                continue
            records = self.ifind.stock_history(symbol, chunk_start, chunk_end)
            rows = _stock_rows(records, trade_dates) if records else []
            missing = want - {row["trade_date"] for row in rows}
            if missing:
                logger.warning(
                    "%s 在 %s~%s 缺 %d 个交易日，疑似被抽样截断",
                    code,
                    chunk_start,
                    chunk_end,
                    len(missing),
                )
            if not rows:
                continue
            with session_scope() as session:
                # upsert_fill 而不是 upsert：上游偶发缺列（开/高/低/收/量）时，
                # merge 会把已采到的非空值刷成 NULL —— 缺数据能等下次补，
                # 抹掉的数据只能重新回补，代价完全不同（2026-10-10 修）
                written += upsert_fill(session, StockDaily, rows)
                # 顺手把名称记进 stock_basic，自选股列表才能显示中文名
                name = next((r["name"] for r in rows if r["name"]), None)
                if name:
                    upsert(session, StockBasic, [{"code": code, "name": name}])
        return written

    def sync_watchlist(
        self, days: int = STOCK_DAILY_DAYS, codes: list[str] | None = None
    ) -> int:
        """同步自选股近期日线，是每日采集的一部分。

        默认只回看最近几天：当日采集只需要覆盖最新交易日，不必每次拉全程。

        `codes` 不给就同步**全部会员自选股的并集**（每日采集走这条）；给了就只同步
        这些（`/api/watchlist/sync` 传当前用户自己的票）。

        ⚠️ 并集必须 `distinct()`：自选股从 2026-09-28 起按用户隔离，同一只票在表里
        会有多行。不去重就会同一只票同步好几遍 —— 那是纯浪费 iFinD 调用次数。

        ⚠️ **失败要报出来**（2026-10-10 修）：原来逐只 `except` 只打日志、最终照旧
        返回行数 —— 全失败时返回 0、这一步仍记 `ok`，而 `has_collected` 据此判定
        「当天已完成」，这一天再也不会重试。所以：**全部失败**直接抛 `IfindError`
        （`_step` 记 failed、`/api/watchlist/sync` 回 400），**部分失败**把「失败 N 只」
        写进步骤 message（经 `_step_note`，见 `_step`）。

        ⚠️ **连续失败要熔断**（2026-10-10 加）：单只 iFinD 超时最长可达约 3 分钟，
        几十只票连着超时会让整轮持锁几个小时（其它采集全部 `CollectionBusy`）。
        所以连着 `WATCHLIST_CONSECUTIVE_FAILURES` 只都失败就**中止本轮**并抛错 ——
        源整体不可用时没必要一只只耗下去。
        """
        if codes is None:
            with session_scope() as session:
                codes = list(session.scalars(select(Watchlist.code).distinct()))
        if not codes:
            return 0

        written = 0
        failed: list[str] = []
        consecutive = 0
        aborted = False
        for code in codes:
            try:
                written += self.sync_stock(code, days=days)
                consecutive = 0
            except Exception as exc:  # noqa: BLE001 - 单只失败不影响其它自选股
                failed.append(code)
                consecutive += 1
                logger.warning("同步自选股 %s 失败: %s", code, exc)
                if consecutive >= WATCHLIST_CONSECUTIVE_FAILURES:
                    logger.warning(
                        "自选股连续 %d 只同步失败，中止本轮（已成功 %d 只 / 共 %d 只）",
                        consecutive,
                        len(codes) - len(failed),
                        len(codes),
                    )
                    aborted = True
                    break
        if aborted or (failed and len(failed) == len(codes)):
            # 熔断 / 全失败多半是 iFinD 整体不可用（401 / 配额 / 网络），
            # 绝不能返回 0 还记 ok —— 抛出来让 `_step` 记 failed、好重试
            raise IfindError(
                f"自选股同步失败（成功 {len(codes) - len(failed)}/{len(codes)} 只）："
                f"{failed[:5]}"
            )
        if failed:
            # 部分失败：借 `_step_note` 把话带到 collect_log.message（页面可见）
            self._step_note = f"失败 {len(failed)} 只（共 {len(codes)} 只）：{failed[:5]}"
        return written

    def collect_sentiment(self, trade_date: date, *, history: bool = False) -> int:
        """汇总当日情绪指标。

        数据源限制：乐咕乐股的涨跌家数、iFinD 的实时涨幅**都只提供当日值**，
        对历史日期取数会把今天的数据写到过去。所以：
        - history=False（当日采集）：全量计算
        - history=True（历史回补）：这两项留空，其余指标来自可按日期取数的
          akshare 涨停三池与指数表，仍然是可信的

        家数取值严格依赖三池的采集状态：某池当日 `pool_*` 日志不是 ok
        （失败或超出数据源窗口），对应计数写 None 而非 0 ——
        0 会被读成「当天没有跌停」，与事实相反。

        ⚠️ **历史回补不能把已有情绪抹空**（2026-10-10 修）：`history=True` 时
        涨跌家数 / 涨跌超 5% 家数 / 打板效应本来就取不到（只有当日值），固定算 None；
        若照旧用 `upsert`（= merge）写库，**None 会把库里已有的值覆盖成空**，
        而且不可逆。所以这一处改用 `upsert_fill`（None 不覆盖已有非空值）。
        另外三池状态改成「当天全部日志里**取最优**（ok > skipped）」：历史回补跑到
        一个已超出三池窗口的日子会补写一条 `skipped`，不能让它把更早采到的 `ok`
        顶掉（顶掉后涨停/跌停/炸板数会被算成 None）。
        """
        if not history:
            _require_today(trade_date, "情绪指标")

        with session_scope() as session:
            # 每池取**最优**状态（ok 优先于 skipped/failed），而不是只看最新一条：
            # 只认最新的话，回补时补写的 skipped 会把先前的 ok 顶掉（见 docstring）。
            # ok 存在时 LimitPool 里就有那天的行，重算得到的计数仍然是对的。
            statuses = session.execute(
                select(CollectLog.task, CollectLog.status).where(
                    CollectLog.trade_date == trade_date,
                    CollectLog.task.in_([f"pool_{name}" for name in POOL_TYPES]),
                )
            ).all()
            ok_pools = {
                task.removeprefix("pool_")
                for task, status in statuses
                if status == "ok"
            }
            pools = session.execute(
                select(LimitPool.pool_type, LimitPool.consecutive).where(
                    LimitPool.trade_date == trade_date
                )
            ).all()
            amounts = (
                session.scalars(
                    select(IndexDaily.amount).where(
                        IndexDaily.trade_date == trade_date,
                        IndexDaily.code.in_(AMOUNT_SYMBOLS),
                    )
                ).all()
            )

        counts: dict[str, int | None] = {
            pool_type: (0 if pool_type in ok_pools else None)
            for pool_type in POOL_TYPES
        }
        consecutive: list[int] = []
        for pool_type, value in pools:
            if counts.get(pool_type) is not None:
                counts[pool_type] += 1  # type: ignore[operator]
            if pool_type == "up" and value:
                consecutive.append(int(value))

        # 乐咕的涨跌家数与打板效应都**只有当日值**，且任一失败都不该让整条情绪不落库
        # ——各自动 try，互不影响（2026-10-10 修）
        activity: dict = {}
        if not history:
            try:
                activity = self.ak.market_activity()
            except Exception as exc:  # noqa: BLE001 - 涨跌家数失败不该拖垮整条情绪
                logger.warning("涨跌家数取数失败（%s）：%s", trade_date, exc)
        # 涨跌超 5% 的家数：乐咕那份宽度没有分档，只能问 iFinD 选股（每次 1 问，
        # 取 `matched` 当计数——实测「涨幅大于5%的A股股票」matched=550）。
        # 与涨跌家数一样**只有当日值**，所以 history=True 时留空
        up5_count, down5_count = (None, None) if history else self._five_percent_counts(trade_date)
        yesterday_avg: float | None = None
        if not history:
            try:
                yesterday_avg = self._yesterday_limit_effect(trade_date)
            except Exception as exc:  # noqa: BLE001 - 打板效应失败不该拖垮整条情绪
                logger.warning("昨日涨停今日均涨取数失败（%s）：%s", trade_date, exc)
        amount_values = [a for a in amounts if a]
        payload = build_sentiment(
            trade_date,
            limit_up_count=counts["up"],
            limit_down_count=counts["down"],
            broken_count=counts["broken"],
            consecutive_list=consecutive,
            # 涨跌家数取乐咕乐股全市场宽度；iFinD 只对上证指数有效
            up_count=to_int(activity.get("上涨")),
            down_count=to_int(activity.get("下跌")),
            up5_count=up5_count,
            down5_count=down5_count,
            total_amount=sum(amount_values) if amount_values else None,
            yesterday_limit_today_avg=yesterday_avg,
        )
        with session_scope() as session:
            # upsert_fill：历史回补算出来的 None 不覆盖库里已有值（见 docstring）
            return upsert_fill(session, MarketSentiment, [payload])

    # ------------------------------------------------------------------ 衍生值

    def _five_percent_counts(self, trade_date: date) -> tuple[int | None, int | None]:
        """涨超 5% / 跌超 5% 的**全市场**家数（涨的含涨停）。

        为什么不用本地 `stock_daily` 数：它只有池子（成交额 ≥ 1 亿的 3032 只，
        占全市场约一半只数），而涨停股实测有 37% 在池外 —— 拿池子算会系统性
        低估，和旁边的「涨跌家数」（全市场口径）放在一起还会显得自相矛盾。

        两问各 1 次调用，取 `matched`（匹配总数）而不是返回行数：表格只给 100 行，
        `matched` 才是真的家数。任一问失败就都记 None —— 只写一半会让人以为
        「那天跌超 5% 的只有 0 只」。
        """
        _require_today(trade_date, "涨跌超5%家数")
        day = f"{trade_date.year}年{trade_date.month}月{trade_date.day}日"
        try:
            up = self.ifind.search_stocks(f"{day}涨幅大于5%的A股股票")["matched"]
            down = self.ifind.search_stocks(f"{day}跌幅大于5%的A股股票")["matched"]
        except Exception as exc:  # noqa: BLE001 - 情绪采集不该因为这两问整轮失败
            logger.warning("涨跌超5%家数取数失败：%s", exc)
            return None, None
        return up, down

    def _yesterday_limit_effect(self, trade_date: date) -> float | None:
        """昨日涨停股今日均涨幅 —— 打板赚钱效应。

        只存自选股与选股结果的行情，所以这里临时向 iFinD 取昨日涨停股
        的今日涨跌幅（单次上限 10 只，stock_quotes 内部自动分片）。
        """
        _require_today(trade_date, "昨日涨停股今日涨幅")
        with session_scope() as session:
            prev_date = session.scalar(
                select(TradeCalendar.trade_date)
                .where(TradeCalendar.trade_date < trade_date)
                .order_by(TradeCalendar.trade_date.desc())
                .limit(1)
            )
            if prev_date is None:
                return None
            codes = list(
                session.scalars(
                    select(LimitPool.code).where(
                        LimitPool.trade_date == prev_date,
                        LimitPool.pool_type == "up",
                    )
                )
            )

        if not codes:
            return None

        records = self.ifind.stock_quotes([to_ths_symbol(c) for c in codes], ["涨跌幅"])
        values = [
            value
            for value in (pick_float(record, "涨跌幅") for record in records)
            if value is not None
        ]
        if not values:
            return None
        return round(sum(values) / len(values), 2)

    # ---------------------------------------------------------------- 资金面

    def collect_margin(self, trade_date: date) -> int:
        """两融：融资余额 / 融资买入额 / 融券余额（iFinD EDB，1 次调用）。"""
        return collect_funds.collect_margin(self.ifind, trade_date)

    def collect_hsgt(self, trade_date: date) -> int:
        """沪深股通成交额（iFinD EDB，1 次调用）。"""
        return collect_funds.collect_hsgt(self.ifind, trade_date)

    def collect_etf(self) -> int:
        """ETF 份额与行情（同花顺清单 + iFinD 份额/行情，约 20 次 iFinD 调用）。

        **不接受目标日期**：落库日期由同花顺自带的「最新-交易日」决定 ——
        盘前/凌晨/周末取到的都是上一交易日的快照，按「今天」写会造出假数据点。
        详见 `collect_funds.collect_etf`。

        配额到 80% 就让路（与形态日线、DDE 扫描同档）：它属于增强项，
        指数 / 涨停 / 板块 / 情绪那条主线才是复盘的地基。
        """
        level = quota_level(date.today(), self.settings)
        if level >= QuotaLevel.PAUSE_KLINE:
            logger.warning("ETF 采集跳过：%s", level_label(level))
            return 0
        return collect_funds.collect_etf(self.ak, self.ifind)

    def collect_lhb_institution(self, trade_date: date) -> int:
        """龙虎榜机构席位统计（akshare，零配额）。"""
        return collect_funds.collect_lhb_institutions(self.ak, trade_date)

    # -------------------------------------------------------------------- 编排

    def _log(
        self,
        trade_date: date | None,
        task: str,
        status: str,
        rows: int,
        message: str | None,
        cost: float,
    ) -> None:
        """写一条采集日志。status 取值：ok / failed / skipped。"""
        with session_scope() as session:
            session.add(
                CollectLog(
                    trade_date=trade_date,
                    task=task,
                    status=status,
                    rows=rows,
                    message=message,
                    cost_seconds=cost,
                )
            )

    def _step(self, trade_date: date | None, name: str, fn) -> dict:
        global _current_step
        started = time.monotonic()
        status, row_count, message = "ok", 0, None
        # 步骤自己可以往 `_step_note` 写一句说明（如自选股「失败 3 只」）——
        # 成功但部分失败时用它填 message，让 `collect_log` 直接可见。
        self._step_note = None
        _current_step = name  # 供采集锁的「持有过久」告警读出卡在了哪一步
        try:
            row_count = fn()
            if self._step_note:
                message = self._step_note
        except Exception as exc:  # noqa: BLE001 - 单步失败必须不阻塞后续步骤
            status = "failed"
            message = f"{type(exc).__name__}: {exc}"
            logger.exception("采集步骤 %s 失败", name)
        finally:
            self._step_note = None
            _current_step = None

        cost = round(time.monotonic() - started, 2)
        # ⚠️ 落库的计数必须是**整数**：`collect_log.rows` 是 Integer 列，而有的步骤返回的是
        # 「分口径的行数」字典（板块资金流是 `{taxonomy: 行数}`）。2026-09-22 实测到后果 ——
        # 把字典直接塞进列里，SQLite 抛 `Error binding parameter 4`，而**下面写日志那行
        # 在 try 之外**，于是整条采集链在这一步断掉：flows 之后的龙虎榜 / ETF / 题材 /
        # 板块 / 自选股 / 两融 / 情绪全都没采，日志里只剩一句「定时采集失败」。
        # 日志里仍打原值（字典），只是落库取总和 —— 明细比总数有用。
        rows = sum(row_count.values()) if isinstance(row_count, dict) else row_count
        logger.info("采集 %-10s [%s] rows=%s cost=%.2fs", name, status, row_count, cost)
        try:
            self._log(trade_date, name, status, rows, message, cost)
        except Exception:  # noqa: BLE001 - 记日志失败同样不能拖垮整条链（见上）
            logger.exception("写采集日志失败：%s", name)
        return {"status": status, "rows": rows, "cost": cost, "message": message}

    def _trade_dates(self, start: date, end: date) -> list[date]:
        def _query() -> list[date]:
            with session_scope() as session:
                return list(
                    session.scalars(
                        select(TradeCalendar.trade_date)
                        .where(
                            TradeCalendar.trade_date >= start,
                            TradeCalendar.trade_date <= end,
                        )
                        .order_by(TradeCalendar.trade_date)
                    )
                )

        dates = _query()
        if not dates:
            self.collect_calendar()
            dates = _query()
        return dates

    def backfill_index(self, start: date, end: date | None = None) -> int:
        """用 iFinD `index_data` 回补指数历史。

        `index_highfreq_quotes` 只能取当日快照，历史必须走自然语言接口 `index_data`。
        逐指数分别提问：自然语言接口对多主体的解析不如结构化接口稳。
        """
        end = end or self.latest_trade_date()
        trade_dates = set(self._trade_dates(start, end))
        if not trade_dates:
            return 0

        # 已有数据的日期跳过。这里必须跳过而非覆盖：upd 写入的行带
        # up_count 等 None，覆盖会把当日由高频接口取到的涨跌家数抹掉。
        with session_scope() as session:
            existing = {
                (row.trade_date, row.code)
                for row in session.scalars(
                    select(IndexDaily).where(
                        IndexDaily.trade_date >= start,
                        IndexDaily.trade_date <= end,
                    )
                )
            }

        written = 0
        for symbol, name in INDEX_NAMES.items():
            rows: list[dict] = []
            asked = False
            for chunk_start, chunk_end in _date_chunks(
                start, end, INDEX_BACKFILL_CHUNK_DAYS
            ):
                expected = {d for d in trade_dates if chunk_start <= d <= chunk_end}
                # 这一块的交易日全在库里就**不必问**：去重发生在取数之后，
                # 不先跳的话「重复跑不浪费配额」是假的 —— 照样会问一遍才发现
                # 一行都用不上（6 个指数 × 每块 1 次）
                if expected and all((d, symbol) in existing for d in expected):
                    continue
                _, records = self.ifind.index_data(
                    f"{name} 从{chunk_start.isoformat()}到{chunk_end.isoformat()}"
                    f"每个交易日的收盘价、涨跌幅、成交额"
                )
                asked = True
                chunk_rows = _index_nl_rows(records, trade_dates)
                rows.extend(chunk_rows)
                # 逐块对账：接口每次只给 100 行，区间超了会**抽样截断**，
                # 而返回的行看起来完全正常 —— 只有跟交易日历比才知道少了天
                missing = expected - {row["trade_date"] for row in chunk_rows}
                if missing:
                    logger.warning(
                        "回补指数 %s %s~%s 缺 %d 个交易日（接口 100 行上限导致的抽样？）",
                        name,
                        chunk_start,
                        chunk_end,
                        len(missing),
                    )

            if not asked:
                continue
            if not rows:
                # iFinD 对识别不出的主体静默丢弃，不校验会以为补全了
                logger.warning("回补指数 %s 未返回任何数据，请检查主体名称", name)
                continue

            fresh = [
                row for row in rows if (row["trade_date"], row["code"]) not in existing
            ]
            if not fresh:
                continue
            with session_scope() as session:
                written += upsert(session, IndexDaily, fresh)
            logger.info("回补指数 %-8s %d 行", name, len(fresh))
        return written

    def backfill(self, start: date, end: date | None = None) -> dict:
        """回补历史数据：指数行情 + 涨停三池 + 龙虎榜 + 情绪指标。

        可回补：指数日线（走 iFinD `index_data`）、涨停数、跌停数、炸板数、
        封板率、炸板率、最高连板、两市成交额、龙虎榜。

        窗口受限：跌停池与炸板池的数据源只保留最近 30 个自然日，
        更早的日期这两个指标会记 None（不是 0），详见 `POOL_HISTORY_DAYS`。

        无法回补：涨跌家数（乐咕乐股仅返回当日）、打板效应（依赖当日实时涨幅）、
        指数涨跌家数（iFinD 两种接口对同一指数同一天给出的数值不一致）。
        """
        end = end or self.latest_trade_date()

        # 指数必须最先补：历史情绪的「两市成交额」取自指数表，顺序反了就取不到
        index_step = self._step(
            None, "index_history", lambda: self.backfill_index(start, end)
        )
        # 龙虎榜接口原生支持区间查询，一次取一个月，不必逐日调用
        lhb_step = self._step(
            None, "lhb_history", lambda: self.backfill_lhb(start, end)
        )

        days = []
        for target in self._trade_dates(start, end):
            days.append(
                {
                    "trade_date": target.isoformat(),
                    "limit_pool": self._step(
                        target, "limit_pool", lambda t=target: self.collect_limit_pool(t)
                    ),
                    "sentiment": self._step(
                        target,
                        "sentiment",
                        lambda t=target: self.collect_sentiment(t, history=True),
                    ),
                }
            )
        return {"index_history": index_step, "lhb_history": lhb_step, "days": days}

    def backfill_lhb(self, start: date, end: date) -> int:
        """按区间回补龙虎榜。每行自带上榜日，按日期分片写入。"""
        # 交易日集合用于剔除接口可能返回的非交易日行
        trade_dates = set(self._trade_dates(start, end))
        written = 0
        for chunk_start, chunk_end in _date_chunks(start, end, LHB_BACKFILL_CHUNK_DAYS):
            records = self.ak.lhb_range(chunk_start, chunk_end)
            rows = _lhb_rows(chunk_start, records)
            fresh = [row for row in rows if row["trade_date"] in trade_dates]
            if not fresh:
                continue
            with session_scope() as session:
                written += upsert(session, Lhb, fresh)
        return written

    def run(self, trade_date: date | None = None) -> dict:
        """执行一次完整采集，返回各步骤摘要。"""
        steps: dict[str, dict] = {}
        steps["calendar"] = self._step(None, "calendar", self.collect_calendar)

        target = trade_date or self.latest_trade_date()

        # 配额让路的第三档：只保留复盘主线。指数、涨停三池、情绪是「当天不看就
        # 永远看不到」的数据 —— 三池的数据源只留 15 个交易日，漏了就补不回来。
        # 而题材、板块、自选股日线都能在配额恢复后重采，所以先停它们。
        core_only = quota_level(settings=self.settings) >= QuotaLevel.CORE_ONLY
        if core_only:
            logger.warning("配额已达 95%，本次只采指数 / 涨停三池 / 情绪主线")

        steps["index"] = self._step(target, "index", lambda: self.collect_index(target))
        steps["limit_pool"] = self._step(
            target, "limit_pool", lambda: self.collect_limit_pool(target)
        )
        # 涨停原因走同花顺数据中心，零 iFinD 配额，任何档位都照采；
        # 排在涨停池之后是为了能跟涨停池的家数对账
        steps["reasons"] = self._step(target, "reasons", lambda: self.collect_reasons(target))
        # 板块资金流**不再走同花顺**（2026-09-23 起改用开盘啦口径：板块成分股 ×
        # 逐股净流入，见 `jobs/collect_board_flow.py`）——它依赖 DDE 那一步的逐股
        # 净流入，所以不在这个采集链里，而是排在调度器的最后（`_collect_board_flow`）
        # 龙虎榜走 akshare，不占 iFinD 配额，任何档位都照采
        steps["lhb"] = self._step(target, "lhb", lambda: self.collect_lhb(target))
        # 情绪只依赖涨停池+指数，必须尽早落库：首页「今日复盘」以 market_sentiment
        # 有没有数据为准。若把情绪放在 ETF（约 20 次 iFinD）之后，ETF 一卡死
        # 页面会一直显示「暂无数据」，尽管指数/涨停其实已经采完。
        steps["sentiment"] = self._step(
            target, "sentiment", lambda: self.collect_sentiment(target)
        )
        # ETF 换 iFinD 源之后要花约 20 次调用（配额让路在它自己内部判断），
        # 龙虎榜机构席位仍是 akshare、零配额
        steps["etf"] = self._step(target, "etf", self.collect_etf)
        steps["lhb_institution"] = self._step(
            target, "lhb_institution", lambda: self.collect_lhb_institution(target)
        )
        if not core_only:
            # 涨停题材（开盘红涨停天梯 → `stock_concept`）**不在这里**：开盘红当天要
            # 晚些才更新天梯，15:05 问回来的是**上一交易日**的那批（payload 的 Date
            # 会明说，见 `sources/kaipanhong.limit_up_ladder`），所以挪到 17:30 那一趟
            # （`run_late`）。2026-09-30 之前放这里，结果 09-28 起三天的板块归属
            # 全是前一天的数据，首板全没有板块。
            # 板块行情（开盘红口径）—— 这个**不滞后**：实测 15:05 问当天给的就是当天
            # （09-30 医药 +1.545% 对 09-29 锂电池 +1.222%，逐项不同）
            steps["sectors"] = self._step(
                target, "sectors", lambda: self.collect_sectors(target)
            )
            # 自选股日线：每天跟着刷新，否则自选股页显示的还是上次看的价格
            steps["watchlist"] = self._step(target, "watchlist", self.sync_watchlist)
            # 资金面（两融、北向成交额）：EDB 有长历史，配额紧张时可以让路 ——
            # 与涨停三池不同，漏掉一天下次还能补回来
            steps["margin"] = self._step(
                target, "margin", lambda: self.collect_margin(target)
            )
            steps["hsgt"] = self._step(target, "hsgt", lambda: self.collect_hsgt(target))
        return {"trade_date": target.isoformat(), "steps": steps}

    def run_late(self, trade_date: date | None = None) -> dict:
        """只跑「收盘后才有数据」的那几步（17:30 那一趟）。

        **背景**（2026-09-30 加）：采集时刻 09-28 从 17:30 提到 15:05 之后，
        **龙虎榜与机构席位从那天起再没入过库** —— 这两个来源按日期问、没有回看窗口，
        15:05 问它当天就是「暂无数据」，第二天也不会替它补（实测两张表停在 09-28，
        而涨停三池当天就有）。用户当时要的只是「**推送**不等龙虎榜」。

        两融 / 北向本来有回看、漏一天下次自动补，放这一趟是为了把「滞后一天」收掉。

        **涨停题材（开盘红天梯）也在这一趟**：它同样是「问当天给上一天」（当天要晚些
        才更新，见 `sources/kaipanhong.limit_up_ladder`），放在 15:05 会把昨天的板块
        安到今天的票上 —— 首板全没有板块（2026-09-30 修，用户就是从这看出来的）。

        **不重跑 15:05 那套**：指数 / 三池 / 情绪 / 板块行情都已在库，重跑白花约 50 次调用。

        配额：龙虎榜、机构席位、涨停题材零 iFinD 配额（akshare / 开盘红），任何档位都采；
        两融 / 北向与 `run()` 一样受 CORE_ONLY 约束（它们能回看，真让路也补得回来）。
        """
        target = trade_date or self.latest_trade_date()
        steps: dict[str, dict] = {}
        steps["lhb"] = self._step(target, "lhb", lambda: self.collect_lhb(target))
        steps["lhb_institution"] = self._step(
            target, "lhb_institution", lambda: self.collect_lhb_institution(target)
        )
        steps["themes"] = self._step(target, "themes", lambda: self.collect_themes(target))
        if quota_level(settings=self.settings) < QuotaLevel.CORE_ONLY:
            steps["margin"] = self._step(target, "margin", lambda: self.collect_margin(target))
            steps["hsgt"] = self._step(target, "hsgt", lambda: self.collect_hsgt(target))
        else:
            logger.warning("配额已达 95%，收盘后这一趟只采零配额那三项（龙虎榜 / 机构席位 / 题材）")
        return {"trade_date": target.isoformat(), "steps": steps}
