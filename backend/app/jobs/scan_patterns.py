"""全市场形态扫描：读日线 → 复权 → 逐形态判定 → 落 `pattern_hit`。

**这一步不花 iFinD 配额** —— 形态全在本地算，实测 3032 只 0.7 秒。成本只跟
池子大小挂钩，加再多形态都是 0 次调用。所以它是整条链路里最便宜的一环，
也是唯一可以随便重跑的一环。

## 为什么整段重算而不是增量

形态看的是「最近 N 根 K 线的形状」，而 N 最长 249。今天补上一根新 K 线，
昨天那些票的形态可能全都变了（也可能没变）。增量更新得知道「哪些票的形状
被新数据影响了」，而这个问题没有便宜的解。整段重算是 0.7 秒，不值得为它做增量。

## 为什么先删后插

同一天重复扫描必须幂等 —— 手工补扫、定时任务撞车都会发生。所以按
`trade_date` 整段替换，而不是 upsert：**形态是会消失的**。昨天命中「平台突破」
的票今天可能已经跌回平台里，upsert 会把旧命中永久留在表里，榜单越看越假。
"""

import logging
import time
from collections import defaultdict
from datetime import date

from sqlalchemy import delete, func, select

from app.config import Settings, get_settings
from app.db import session_scope, upsert_many
from app.jobs.collect_universe import load_codes
from app.models import CollectLog, PatternHit, StockDaily, TradeCalendar
from app.services.patterns import MIN_SCORE, Bars, build_bars, compute_rs, evaluate
from app.sources.ifind import IfindError

logger = logging.getLogger(__name__)

# 扫描时每只票取多少根 K 线。要够 249 日新高用（最长窗口 + 1），再留一点余量
SCAN_BARS = 260

# 采集日志里的一类任务名
PATTERN_TASK = "patterns"


def already_scanned(trade_date: date) -> bool:
    """该交易日是否已经扫过。

    只用来在日志上留个记号并避免同一轮里重复打印，**不承担正确性职责** ——
    重扫是幂等的、只要 0.7 秒、还零配额，所以「多扫一次」没有代价。
    """
    return _scanned_marker(trade_date)


def _scanned_marker(trade_date: date) -> bool:
    with session_scope() as session:
        return bool(
            session.scalar(
                select(CollectLog.trade_date)
                .where(
                    CollectLog.trade_date == trade_date,
                    CollectLog.task == PATTERN_TASK,
                    CollectLog.status == "ok",
                )
                .limit(1)
            )
        )


def _record(
    trade_date: date, status: str, rows: int, message: str | None, cost: float | None = None
) -> None:
    with session_scope() as session:
        session.add(
            CollectLog(
                trade_date=trade_date,
                task=PATTERN_TASK,
                status=status,
                rows=rows,
                message=message,
                cost_seconds=cost,
            )
        )


def _load_bars(trade_date: date, settings: Settings) -> dict[str, list[dict]]:
    """取出最近 `SCAN_BARS` 个交易日内的**股票池**日线，按代码分组。

    一定要按池子过滤，不能把 `stock_daily` 里的票全拿来扫：库里还有一批
    **池外的涨停股**（见 8.22.4，每天只给它们补当天那一根）。它们的序列是断的 ——
    连着两天涨停才有两行相邻，其余日子是空的 —— 而形态引擎会把**相邻的行**
    当成**相邻的交易日**，等于喂进去一条带空洞的 K 线，正是这套引擎最怕的输入。
    """
    universe = load_codes()
    if not universe:
        raise IfindError("股票池为空，先建池（UniverseCollector.collect）")

    with session_scope() as session:
        dates = list(
            session.scalars(
                select(TradeCalendar.trade_date)
                .where(TradeCalendar.trade_date <= trade_date)
                .order_by(TradeCalendar.trade_date.desc())
                .limit(SCAN_BARS)
            )
        )
        if not dates:
            raise IfindError(f"{trade_date} 之前没有交易日历数据")
        start = min(dates)

        rows = session.execute(
            select(
                StockDaily.code,
                StockDaily.name,
                StockDaily.trade_date,
                StockDaily.open,
                StockDaily.high,
                StockDaily.low,
                StockDaily.close,
                StockDaily.volume,
                StockDaily.amount,
                StockDaily.pct_chg,
            )
            .where(
                StockDaily.trade_date >= start,
                StockDaily.trade_date <= trade_date,
                StockDaily.code.in_(universe),
            )
            .order_by(StockDaily.code, StockDaily.trade_date)
        ).all()

    grouped: dict[str, list[dict]] = defaultdict(list)
    for code, name, day, open_, high, low, close, volume, amount, pct in rows:
        # 涨跌幅为空的行在采集时就已经滤掉了，这里再挡一道：
        # 少了它 build_bars 会把停牌日当成 0% 涨跌，前复权序列直接失真
        if close is None or pct is None:
            continue
        grouped[code].append(
            {
                "date": day,
                "name": name,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
                "pct_chg": pct,
            }
        )
    return grouped


def scan(
    trade_date: date | None = None,
    settings: Settings | None = None,
    *,
    min_score: float = MIN_SCORE,
) -> dict:
    """扫描一个交易日的全市场形态，整段替换该日的命中记录。"""
    settings = settings or get_settings()
    started = time.monotonic()

    target = trade_date or _latest_trade_date()
    _require_bars(target)
    grouped = _load_bars(target, settings)
    if not grouped:
        raise IfindError(f"{target} 没有日线数据，先跑 collect_kline")

    rows: list[dict] = []
    skipped = 0
    by_pattern: dict[str, int] = defaultdict(int)
    # 先把全市场的 K 线都建出来、再统一算 RS 评级 —— 它是**横截面排名**，
    # 单只票自己算不出来，必须等所有票都在手上（见 patterns.compute_rs）
    bars_map: dict[str, Bars] = {}
    for code, records in grouped.items():
        if len(records) < 60:
            # 次新股、长期停牌：K 线不够长，任何形态都判不出来
            skipped += 1
            continue
        bars_map[code] = build_bars(records)
    compute_rs(bars_map)

    for code, bars in bars_map.items():
        records = grouped[code]
        last = records[-1]
        for signal in evaluate(bars, min_score=min_score):
            by_pattern[signal.pattern] += 1
            rows.append(
                {
                    "trade_date": target,
                    "code": code,
                    "pattern": signal.pattern,
                    "name": last["name"],
                    "score": round(signal.score, 1),
                    "close": last["close"],
                    "pct_chg": last["pct_chg"],
                    "amount": last["amount"],
                    "key_levels": signal.key_levels,
                    "detail": signal.detail,
                }
            )

    with session_scope() as session:
        session.execute(delete(PatternHit).where(PatternHit.trade_date == target))
        written = upsert_many(session, PatternHit, rows)

    cost = round(time.monotonic() - started, 2)
    logger.info(
        "形态扫描完成：%s，%d 只票 → %d 条命中（%d 只因 K 线不足跳过），用时 %ss",
        target,
        len(grouped),
        written,
        skipped,
        cost,
    )
    _record(target, "ok", written, f"{len(grouped)} 只 / {written} 条命中", cost)
    return {
        "status": "ok",
        "trade_date": target.isoformat(),
        "codes": len(grouped),
        "skipped": skipped,
        "rows": written,
        "by_pattern": dict(by_pattern),
        "cost_seconds": cost,
    }


def _require_bars(trade_date: date) -> None:
    """确认库里真的有这一天的日线。

    少了这道校验，扫描会拿**昨天**的 K 线当今天用 —— 结果不是空的，而是「用
    昨天的数据打上今天的日期」，看起来完全正常。这种错误没有任何外部症状，
    只会在事后复盘时发现「那天的信号怎么是用前一天的价算的」。
    """
    with session_scope() as session:
        latest = session.scalar(select(func.max(StockDaily.trade_date)))
        count = (
            session.scalar(
                select(func.count())
                .select_from(StockDaily)
                .where(StockDaily.trade_date == trade_date)
            )
            or 0
        )
    if latest is None:
        raise IfindError("stock_daily 是空的，先跑 collect_kline")
    if latest < trade_date or count == 0:
        raise IfindError(f"{trade_date} 的日线还没采到（库里最新是 {latest}），先跑 collect_kline")


def _latest_trade_date() -> date:
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
