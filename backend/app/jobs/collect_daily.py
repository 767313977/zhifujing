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
from datetime import date

from sqlalchemy import select
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
from app.sources.markdown_table import pick_float, pick_int, pick_text, to_int

logger = logging.getLogger(__name__)

# 复盘用核心指数。iFinD 对无法识别的代码会**静默丢弃**，采集后必须校验返回行数。
INDEX_SYMBOLS = ["000001.SH", "399001.SZ", "399006.SZ", "000688.SH", "000852.SH"]
INDEX_INDICATORS = [
    "最新价",
    "涨跌幅",
    "成交额",
    "上涨家数",
    "下跌家数",
    "涨停家数",
    "跌停家数",
]
# 沪深两市成交额 = 上证指数 + 深证成指
AMOUNT_SYMBOLS = ("000001.SH", "399001.SZ")


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

    def collect_limit_pool(self, trade_date: date) -> int:
        rows = (
            _limit_up_rows(trade_date, self.ak.limit_up_pool(trade_date))
            + _limit_down_rows(trade_date, self.ak.limit_down_pool(trade_date))
            + _broken_rows(trade_date, self.ak.broken_pool(trade_date))
        )
        with session_scope() as session:
            return _upsert(session, LimitPool, rows)

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
        """
        if not history:
            _require_today(trade_date, "情绪指标")
        with session_scope() as session:
            pools = session.execute(
                select(LimitPool.pool_type, LimitPool.consecutive).where(
                    LimitPool.trade_date == trade_date
                )
            ).all()
            amounts = session.execute(
                select(IndexDaily.amount).where(
                    IndexDaily.trade_date == trade_date,
                    IndexDaily.code.in_(AMOUNT_SYMBOLS),
                )
            ).scalars().all()

        counts = {"up": 0, "down": 0, "broken": 0}
        consecutive: list[int] = []
        for pool_type, value in pools:
            counts[pool_type] = counts.get(pool_type, 0) + 1
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
        with session_scope() as session:
            session.add(
                CollectLog(
                    trade_date=trade_date,
                    task=name,
                    status=status,
                    rows=row_count,
                    message=message,
                    cost_seconds=cost,
                )
            )
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

    def backfill(self, start: date, end: date | None = None) -> list[dict]:
        """回补涨停三池、龙虎榜与情绪指标（情绪周期曲线需要历史数据）。

        可回补：涨停/跌停/炸板数、封板率、炸板率、最高连板 —— 来自按日期取数的
        akshare 三池，是情绪周期最核心的指标。

        无法回补：指数快照（iFinD 高频接口仅当日）、涨跌家数（乐咕仅当日）、
        打板效应（依赖当日实时涨幅）。历史情绪表里这三项会留空。
        """
        end = end or self.latest_trade_date()
        results = []
        for target in self._trade_dates(start, end):
            results.append(
                {
                    "trade_date": target.isoformat(),
                    "limit_pool": self._step(
                        target, "limit_pool", lambda t=target: self.collect_limit_pool(t)
                    ),
                    "lhb": self._step(target, "lhb", lambda t=target: self.collect_lhb(t)),
                    "sentiment": self._step(
                        target,
                        "sentiment",
                        lambda t=target: self.collect_sentiment(t, history=True),
                    ),
                }
            )
        return results

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
