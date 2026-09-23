"""全市场日线采集（形态选股的数据底座）。

### 为什么是「代码前缀 × 交易日」，而不是「一堆股票 × 一段区间」

iFinD 的表格只给 **100 行**，超了就给一张抽样表，并在回答里写一句「数据被截断」。

2026-09-21 起 `get_stock_performance` **连 CSV 都不给了**（旧文案「数据过大」、
旧注释里「列数 4/5 就给 CSV」都不再成立）—— 于是「50 只 × 9 个交易日」这种原本
完全正常的形状会**静默退回约 100 行的抽样表**。那天 61 批只写回 4558 行（应为
约 27000 行），而日志、页面、形态结果全都看不出异常。

改走选股接口（`search_stocks`）就没这个问题：它按代码前缀分段，结果集给 CSV
（上限 1000 行，切段后每段都够），完备性还能靠 `matched` 校验。问法从
「这些股票在某区间的行情」换成「以 <前缀> 开头的 A 股在**某一天**的行情」——
**一天 13 次调用拿全市场**，比原来的 61 批更省，而且一天一天地问，天然不会有空洞。

### 采哪些票

`stock_universe`（流动性 / 市值筛过的池子）**加上当日涨停三池里池外的那些**。
池子是按流动性/市值筛的，会把小市值的涨停股大量筛掉（实测 09-21：三池 130 只里
48 只不在池内），而那正是「点进涨停复盘想看日 K」最想看的一批。前缀问法本来就要
把全市场拿回来再筛，多留这几十只**不额外花任何配额**。

### 两个必须处理的口径问题

**① 停牌日不是 K 线。** iFinD 对停牌股会拿停牌前的价格把 OHLC 四价填成完全相等、
成交量留空、涨跌幅留空 —— 实测全市场 1167 行、涉及 212 只票。判据**只能是涨跌幅
是否为空**：一字板也是四价相等，但它是真实交易日（实测 1277 行，必须保留）。
另外盘中跑一轮时 iFinD 只给当天的当前价、不给开高低，那是半根 K 线，OHLC 缺一即丢。

**② 只存不复权价 + 真实涨跌幅。** `收盘价` 是不复权的（除权日跳空），而 `涨跌幅`
是按除权调整后的真实收益率。形态引擎在内存里用 `pct_chg` 复利出前复权净值序列 ——
**不落第二份前复权价**，一份库里放两种口径的价格迟早有人拿错。

换源时专门对过账：600000 在 2026-09-18 走选股快照拿到的 OHLC / 涨跌幅 / 成交量 /
成交额 / 换手率，与老路径写进库的值**逐位相同**（含单位），所以这次换源不改口径。
"""

import logging
import time
from datetime import date, timedelta

from sqlalchemy import delete, func, select

from app.config import Settings, get_settings
from app.db import session_scope, upsert_many
from app.jobs.collect_universe import crawl_prefixes, load_codes
from app.models import CollectLog, LimitPool, StockDaily, StockUniverse, TradeCalendar
from app.services.usage import QuotaLevel, quota_level
from app.sources.ifind import IfindClient, IfindError
from app.sources.markdown_table import pick_float, pick_text

logger = logging.getLogger(__name__)

# 采集日志里的一类任务名，只为在「数据管理」页留痕
KLINE_TASK = "kline"

# 判定「该日期的日线已经采到」的覆盖率门槛。池子里的票不可能全都在任一
# 交易日有成交（停牌、次新股），所以不能要求 100%
_COVERED_RATIO = 0.9

# 一次问一天，一次问全市场的一个代码前缀段。选股接口按前缀切段后每段都在
# 1000 行以内，不必再按行数分批。
_SNAPSHOT_COLUMNS = "开盘价、最高价、最低价、收盘价、成交量、成交额、涨跌幅、换手率"

# 某天缺的票超过这个比例，才认为那天是真的没采到（而不是停牌这种正常缺失）。
# 正常交易日实测缺 0.3%（8/3032）；被抽样截断的那天缺 80% 以上 ——
# 隔着两个数量级，取 20% 不会误伤，也不会让「那天没采」被当成正常而跳过。
_REPAIR_MISSING_RATIO = 0.2

# 交易日 → 日历日的放大系数。250 个交易日约合 365 个自然日，
# 再算上春节这种连休，按 1.5 倍取；多出来的会被交易日历滤掉，不会写进库
_CALENDAR_FACTOR = 1.5


def has_bars(trade_date: date) -> bool:
    """该交易日的日线是否已经落到库里。

    直接查库，**不看采集日志**：日志说「跑过了」不等于「数据拿到了」。
    盘中手动跑一轮时，当天不完整的行会被丢弃，此时若按日志判定「已完成」，
    收盘后那轮正常采集就会被跳过 —— 当天永远缺一根 K 线，而且没人会发现。
    """
    with session_scope() as session:
        pool = session.scalar(select(func.count()).select_from(StockUniverse)) or 0
        if not pool:
            return False
        got = (
            session.scalar(
                select(func.count())
                .select_from(StockDaily)
                .where(StockDaily.trade_date == trade_date)
            )
            or 0
        )
    return got >= pool * _COVERED_RATIO


def _record(trade_date: date, status: str, rows: int, message: str | None) -> None:
    with session_scope() as session:
        session.add(
            CollectLog(
                trade_date=trade_date,
                task=KLINE_TASK,
                status=status,
                rows=rows,
                message=message,
            )
        )


def _trade_dates(start: date, end: date) -> list[date]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(TradeCalendar.trade_date)
                .where(
                    TradeCalendar.trade_date >= start, TradeCalendar.trade_date <= end
                )
                .order_by(TradeCalendar.trade_date)
            )
        )


def _snapshot_query(prefix: str, day: date) -> str:
    """问「以 <prefix> 开头的 A 股在 <day> 的行情」。

    日期必须写成 `2026年9月18日` 而不是 `2026-09-18`：自然语言问句里前者的
    解析是稳定的（实测同一问法两遍都拿回 600 段 747/747 只）。

    返回的列名会带日期与口径后缀（`收盘价:不复权[20260918]`、
    `涨跌幅:前复权[20260918]`），所以取值一律用关键词包含匹配。
    """
    return (
        f"证券代码以{prefix}开头的A股股票"
        f"{day.year}年{day.month}月{day.day}日的{_SNAPSHOT_COLUMNS}"
    )


def _snapshot_row(row: dict, code: str, day: date) -> dict | None:
    """选股返回的一行 → `StockDaily` 行。判据与老路径完全一致（见模块说明 ①）。

    取不到 OHLC 或涨跌幅的一律丢掉，**不写半份数据**。
    """
    open_, high = pick_float(row, "开盘价"), pick_float(row, "最高价")
    low, close = pick_float(row, "最低价"), pick_float(row, "收盘价")
    pct_chg = pick_float(row, "涨跌幅")
    if None in (open_, high, low, close) or pct_chg is None:
        return None

    return {
        "trade_date": day,
        "code": code,
        "name": pick_text(row, "证券简称", "股票简称"),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": pick_float(row, "成交量"),
        "amount": pick_float(row, "成交额"),
        "pct_chg": pct_chg,
        "turnover": pick_float(row, "换手率"),
    }


class KlineCollector:
    """全市场日线采集与保留策略。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.ifind = IfindClient(self.settings)

    def collect(
        self, trade_date: date | None = None, *, full: bool = False, max_days: int = 0
    ) -> dict:
        """采一轮日线。

        从**最旧**的交易日往新遍历，**只采库里还缺的那些天**。

        两个性质都是为了「分批补历史」这件事本身：

        - **可续跑**：补过的天直接跳过，所以中断后重跑不会把配额再花一遍
          （`full=True` 覆盖 250 个交易日、约 3500 次调用，一轮配额不够）。
        - **`max_days` 卡的是「本轮补几天」，不是「只看最近几天」**。这点必须说清楚：
          窗口是从旧到新走的，所以每跑一轮就往前推进一段，天然分批；
          如果卡的是「最近 N 天」，那每轮都在看同一段（而且那段已经采全了），
          永远推进不到更深的历史 —— 之前就是这么写的，等于 `--days` 没用。
          走完一轮若还有没补齐的天，会数出来放进 `pending_days`（只查库、不花调用）。
        """
        level = quota_level(settings=self.settings)
        if level >= QuotaLevel.PAUSE_KLINE:
            # 配额紧张时形态选股先让路：它每天要几十次调用，
            # 而基础采集（指数/情绪/板块）不能停
            return {
                "status": "skipped",
                "reason": "配额已达 80%，按分级让路停掉全市场日线更新",
            }

        end = trade_date or self._latest_trade_date()
        universe = self._codes()
        if not universe:
            raise IfindError("股票池为空，先建池（UniverseCollector.collect）")
        # 涨停三池里池外的票也一起采，理由见模块说明
        allowance = set(universe) | set(self._pool_codes(end))

        days = self._target_days(end, full=full)
        if not days:
            raise IfindError(f"{end} 之前没有交易日历数据，先采集交易日历")
        started = time.monotonic()
        written = 0
        fetched: list[date] = []
        skipped = 0
        calls = 0
        failed: list[str] = []
        pending = 0
        for index, day in enumerate(days):
            if max_days and len(fetched) >= max_days:
                # 本轮补满了。剩下的天**只用库里的数据**判断要不要补（不花调用），
                # 这样一次分批跑就能报出「还剩几天」，不必再空跑一轮才知道
                pending = sum(
                    1 for rest in days[index:] if self._needs_day(rest, universe)
                )
                logger.info("本轮补满 %d 天，窗口内还剩 %d 天待补", max_days, pending)
                break
            if not self._needs_day(day, universe):
                skipped += 1
                continue
            try:
                got, used = self._fetch_day(day, allowance)
            except Exception as exc:  # noqa: BLE001 - 单天失败不该拖垮整轮
                failed.append(f"{day} {type(exc).__name__}: {exc}")
                logger.warning("%s 日线采集失败：%s", day, exc)
                continue
            written += got
            calls += used
            fetched.append(day)
            logger.info("%s 写入 %d 行（%d 次调用）", day, got, used)

        pruned = self.prune()
        result = {
            "status": "ok" if not failed else "partial",
            "trade_date": end.isoformat(),
            "codes": len(allowance),
            "days": len(days),
            "fetched_days": [day.isoformat() for day in fetched],
            "skipped_days": skipped,
            # 本轮被 max_days 卡住、还没补完的天数（只查库得出的，不额外花调用）。
            # 为 0 表示窗口内该补的都补完了
            "pending_days": pending,
            "calls": calls,
            "rows": written,
            "failed": failed,
            "pruned": pruned,
            "cost_seconds": round(time.monotonic() - started, 2),
        }
        logger.info(
            "日线采集完成：采 %d 天 / 跳过 %d 天 / 待补 %d 天，%d 行 / %d 次调用，清理 %d 行，失败 %d 天，用时 %ss",
            len(fetched),
            skipped,
            pending,
            written,
            calls,
            pruned,
            len(failed),
            result["cost_seconds"],
        )
        # 有失败的天就不记 ok：记了当天就不会再采，那几天的 K 线会一直缺着
        _record(
            end,
            result["status"],
            written,
            f"{len(fetched)} 天 / {calls} 次调用 / {written} 行"
            + (f"，失败 {len(failed)} 天" if failed else ""),
        )
        return result

    def collect_recent(
        self, trade_date: date | None = None, *, days: int = 60
    ) -> dict:
        """只补「最近 N 个交易日」里还缺的天（从旧到新）。

        辉宾/形态要**连续近期**日线；默认的 `full` 回补从窗口最旧端啃，
        会先填两年前的洞，近期仍不够 20 根量比窗口。此方法专补近端。
        """
        end = trade_date or self._latest_trade_date()
        universe = self._codes()
        if not universe:
            raise IfindError("股票池为空，先建池（UniverseCollector.collect）")
        allowance = set(universe) | set(self._pool_codes(end))
        all_days = _trade_dates(self._window_start(end, full=True), end)
        if not all_days:
            raise IfindError(f"{end} 之前没有交易日历数据，先采集交易日历")
        target_days = all_days[-days:] if len(all_days) > days else all_days

        started = time.monotonic()
        written = 0
        fetched: list[date] = []
        skipped = 0
        calls = 0
        failed: list[str] = []
        for day in target_days:
            if not self._needs_day(day, universe):
                skipped += 1
                continue
            try:
                got, used = self._fetch_day(day, allowance)
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{day} {type(exc).__name__}: {exc}")
                logger.warning("%s 近端日线采集失败：%s", day, exc)
                continue
            written += got
            calls += used
            fetched.append(day)
            logger.info("%s 近端写入 %d 行（%d 次调用）", day, got, used)

        return {
            "status": "ok" if not failed else "partial",
            "trade_date": end.isoformat(),
            "window_days": len(target_days),
            "fetched_days": [day.isoformat() for day in fetched],
            "skipped_days": skipped,
            "calls": calls,
            "rows": written,
            "failed": failed,
            "cost_seconds": round(time.monotonic() - started, 2),
        }

    # ------------------------------------------------------------------ 内部

    def _codes(self) -> list[str]:
        return load_codes()

    def _pool_codes(self, trade_date: date) -> list[str]:
        """当日涨停三池的 6 位代码（去重）。

        取不到（当天三池还没采、或传了历史日期）就返回空 —— 那只是「没有额外的票
        要补」，不该让整轮日线采集失败。
        """
        with session_scope() as session:
            return list(
                session.scalars(
                    select(LimitPool.code)
                    .where(LimitPool.trade_date == trade_date)
                    .distinct()
                )
            )

    def _target_days(self, end: date, *, full: bool) -> list[date]:
        """窗口内要确保「有数据」的交易日，**从旧到新**。

        顺序有意义：从旧的开始走 + `max_days` 卡「本轮补几天」，合起来才是分批
        （每轮往前推进一段）。反过来的话每轮都在看同一段。见 `collect` 的说明。
        """
        return _trade_dates(self._window_start(end, full=full), end)

    def _needs_day(self, day: date, universe: list[str]) -> bool:
        """这一天要不要采。

        库里已有的天数直接跳过 —— 这是省配额与可续跑的关键：日常只有最新那天
        缺数据，其余几天一次查询就跳过了。

        只拿 `stock_universe` 当分母，不含当日涨停池里池外的票：那些票本来就
        不该在历史每一天都有行，算进来会让每一天都显得「缺票」而反复重采。
        """
        with session_scope() as session:
            got = (
                session.scalar(
                    select(func.count(func.distinct(StockDaily.code))).where(
                        StockDaily.trade_date == day, StockDaily.code.in_(universe)
                    )
                )
                or 0
            )
        return len(universe) - got > len(universe) * _REPAIR_MISSING_RATIO

    def _fetch_day(self, day: date, allowance: set[str]) -> tuple[int, int]:
        """采一个交易日：按前缀问全市场，只留该留的票。返回 (行数, 调用次数)。"""
        raw, calls = crawl_prefixes(
            self.ifind, lambda prefix: _snapshot_query(prefix, day), str(day)
        )
        rows: list[dict] = []
        for code, raw_row in raw.items():
            if code not in allowance:
                continue
            row = _snapshot_row(raw_row, code, day)
            if row is not None:
                rows.append(row)

        if not rows:
            logger.warning("%s 一只都没取到行情，可能不是交易日或问法失效", day)
            return 0, calls
        with session_scope() as session:
            return upsert_many(session, StockDaily, rows), calls

    def _latest_trade_date(self) -> date:
        with session_scope() as session:
            found = session.scalar(
                select(TradeCalendar.trade_date)
                .where(TradeCalendar.trade_date <= date.today())
                .order_by(TradeCalendar.trade_date.desc())
                .limit(1)
            )
        if found is None:
            raise IfindError("交易日历为空，先采集交易日历")
        return found

    def _window_start(self, end: date, full: bool) -> date:
        if not full:
            return end - timedelta(days=self.settings.kline_refresh_calendar_days)
        return end - timedelta(days=int(self.settings.kline_history_days * _CALENDAR_FACTOR))

    def prune(self) -> int:
        """只留最近 `kline_keep_days` 个交易日。

        形态最长看 250 日，留 400 个交易日有 1.6 倍余量。不清理的话
        每天按池子大小往上堆，一年就到百万行、几百 MB。

        ⚠️ 现在**重采历史很贵**（一天 13 次调用，250 个交易日要 3000 多次，
        一轮 5000 次的配额都不够），所以剪掉的历史实际上找不回来 ——
        改这个值之前先想清楚。
        """
        keep = self.settings.kline_keep_days
        with session_scope() as session:
            dates = list(
                session.scalars(
                    select(TradeCalendar.trade_date)
                    .where(TradeCalendar.trade_date <= date.today())
                    .order_by(TradeCalendar.trade_date.desc())
                )
            )
        if len(dates) < keep:
            # 日历还没铺满这么久，无从判断边界，宁可不删
            return 0
        cutoff = min(dates[:keep])
        with session_scope() as session:
            result = session.execute(
                delete(StockDaily).where(StockDaily.trade_date < cutoff)
            )
        return int(result.rowcount or 0)
