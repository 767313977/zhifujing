"""收盘后数据采集。

触发时机：交易日 15:05 之后（收盘数据已稳定），也支持手动补数。

设计要点：
- 每步独立事务、独立日志，**单步失败不阻塞后续步骤**
- 数据源分工：iFinD 出指数行情；akshare 出涨停三池、龙虎榜、交易日历、市场活跃度
  （后四类 iFinD 完全没有）
- 情绪指标依赖涨停池与指数，故排在最后
"""

import logging
import time
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import session_scope
from app.models import (
    CollectLog,
    IndexDaily,
    Lhb,
    LimitPool,
    MarketSentiment,
    TradeCalendar,
)
from app.services.sentiment import build_sentiment
from app.sources.akshare_source import AkshareSource
from app.sources.ifind import IfindClient, to_ths_symbol
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

# index_data 单次请求的日期跨度上限（自然语言接口，区间过大容易被截断）
INDEX_BACKFILL_CHUNK_DAYS = 120

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


def _upsert(session: Session, model, rows: list[dict]) -> int:
    """按主键 upsert，保证重复采集幂等。"""
    for row in rows:
        session.merge(model(**row))
    return len(rows)


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
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


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
    return [
        {
            "trade_date": r.get("上榜日") or trade_date,
            "code": str(r["代码"]).zfill(6),
            # 同一股票可能因多条上榜原因重复出现，所以 reason 进主键
            "reason": str(r.get("上榜原因") or "未知"),
            "name": r.get("名称"),
            "close": r.get("收盘价"),
            "pct_chg": r.get("涨跌幅"),
            "net_buy": r.get("龙虎榜净买额"),
            "buy_amount": r.get("龙虎榜买入额"),
            "sell_amount": r.get("龙虎榜卖出额"),
            "interpretation": r.get("解读"),
        }
        for r in records
    ]


class DailyCollector:
    """日线采集器。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.ifind = IfindClient(self.settings)
        self.ak = AkshareSource(self.settings)

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

    # -------------------------------------------------------------------- 步骤

    def collect_calendar(self) -> int:
        dates = self.ak.trade_calendar()
        with session_scope() as session:
            return _upsert(session, TradeCalendar, [{"trade_date": d} for d in dates])

    def collect_index(self, trade_date: date) -> int:
        _require_today(trade_date, "指数快照")
        records = self.ifind.index_quotes(INDEX_SYMBOLS, INDEX_INDICATORS)
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
                    "code": code,
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
            return _upsert(session, IndexDaily, rows)

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

        只有三池全部失败才抛错，让上层 `_step` 记成失败；部分失败不阻塞。
        """
        jobs = (
            ("up", "涨停池", self.ak.limit_up_pool, _limit_up_rows),
            ("down", "跌停池", self.ak.limit_down_pool, _limit_down_rows),
            ("broken", "炸板池", self.ak.broken_pool, _broken_rows),
        )
        cutoff = self._pool_window_cutoff()
        written = 0
        failures: list[str] = []

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
                rows = to_rows(trade_date, records)
            except Exception as exc:  # noqa: BLE001 - 单池失败不阻塞其他池
                message = f"{type(exc).__name__}: {exc}"
                logger.warning("采集 %s %s 失败: %s", label, trade_date, exc)
                self._log(
                    trade_date, task, "failed", 0, message,
                    round(time.monotonic() - started, 2),
                )
                failures.append(f"{label}({message})")
                continue

            with session_scope() as session:
                count = _upsert(session, LimitPool, rows)
            written += count
            self._log(
                trade_date, task, "ok", count, None,
                round(time.monotonic() - started, 2),
            )

        # 只有三池**全部真失败**才算这一步失败；全部超期跳过不算失败
        if len(failures) == len(jobs):
            raise RuntimeError(f"{trade_date} 三池全部采集失败: {'; '.join(failures)}")
        return written

    def collect_lhb(self, trade_date: date) -> int:
        rows = _lhb_rows(trade_date, self.ak.lhb(trade_date))
        with session_scope() as session:
            return _upsert(session, Lhb, rows)

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
        """
        if not history:
            _require_today(trade_date, "情绪指标")

        with session_scope() as session:
            # 只认每个池**最新**一条日志：同一日期可能被采集多次，
            # 旧日志里残留的 ok 会让已超期的池被误判为有数据。
            latest_ids = (
                select(func.max(CollectLog.id))
                .where(
                    CollectLog.trade_date == trade_date,
                    CollectLog.task.in_([f"pool_{name}" for name in POOL_TYPES]),
                )
                .group_by(CollectLog.task)
            )
            ok_pools = {
                task.removeprefix("pool_")
                for task, status in session.execute(
                    select(CollectLog.task, CollectLog.status).where(
                        CollectLog.id.in_(latest_ids)
                    )
                )
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

        activity = {} if history else self.ak.market_activity()
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
            total_amount=sum(amount_values) if amount_values else None,
            yesterday_limit_today_avg=None if history else self._yesterday_limit_effect(trade_date),
        )
        with session_scope() as session:
            return _upsert(session, MarketSentiment, [payload])

    # ------------------------------------------------------------------ 衍生值

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
        started = time.monotonic()
        status, row_count, message = "ok", 0, None
        try:
            row_count = fn()
        except Exception as exc:  # noqa: BLE001 - 单步失败必须不阻塞后续步骤
            status = "failed"
            message = f"{type(exc).__name__}: {exc}"
            logger.exception("采集步骤 %s 失败", name)

        cost = round(time.monotonic() - started, 2)
        logger.info("采集 %-10s [%s] rows=%s cost=%.2fs", name, status, row_count, cost)
        self._log(trade_date, name, status, row_count, message, cost)
        return {"status": status, "rows": row_count, "cost": cost, "message": message}

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
            for chunk_start, chunk_end in _date_chunks(
                start, end, INDEX_BACKFILL_CHUNK_DAYS
            ):
                _, records = self.ifind.index_data(
                    f"{name} 从{chunk_start.isoformat()}到{chunk_end.isoformat()}"
                    f"每个交易日的收盘价、涨跌幅、成交额"
                )
                rows.extend(_index_nl_rows(records, trade_dates))
                if symbol not in {row["code"] for row in rows}:
                    # iFinD 对识别不出的主体静默丢弃，不校验会以为补全了
                    logger.warning("回补指数 %s 未返回任何数据，请检查主体名称", name)

            fresh = [
                row for row in rows if (row["trade_date"], row["code"]) not in existing
            ]
            if not fresh:
                continue
            with session_scope() as session:
                written += _upsert(session, IndexDaily, fresh)
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
                written += _upsert(session, Lhb, fresh)
        return written

    def run(self, trade_date: date | None = None) -> dict:
        """执行一次完整采集，返回各步骤摘要。"""
        steps: dict[str, dict] = {}
        steps["calendar"] = self._step(None, "calendar", self.collect_calendar)

        target = trade_date or self.latest_trade_date()
        steps["index"] = self._step(target, "index", lambda: self.collect_index(target))
        steps["limit_pool"] = self._step(
            target, "limit_pool", lambda: self.collect_limit_pool(target)
        )
        steps["lhb"] = self._step(target, "lhb", lambda: self.collect_lhb(target))
        # 情绪依赖涨停池与指数，放最后
        steps["sentiment"] = self._step(
            target, "sentiment", lambda: self.collect_sentiment(target)
        )
        return {"trade_date": target.isoformat(), "steps": steps}
