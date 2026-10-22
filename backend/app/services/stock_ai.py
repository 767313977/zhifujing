"""个股「AI 研判」：把库里已有的数字交给大模型组织成一段话。

**只喂本站自己的数据**（日线、阶段判定、形态命中、DDE、涨跌停/龙虎榜、市场情绪），
不喂新闻也不让它自由发挥 —— 它不知道当天的消息面，放开写只会编。提示词里明确
写死「本站没有新闻数据」，要求它遇到消息面就说「未纳入」。

**不给交易指令**：不写买卖建议、目标价、止损止盈价位、仓位。只写「什么条件下
判断会失效」，那是可观察的事实条件，不是操作指令。

**按 (代码, 数据日) 缓存**：同一只票同一天只烧一次 token，第二个点按钮的人直接读
缓存（`cached=True`）。缓存键带数据日，采集补进新的一天后自然失效，不需要 TTL 猜。

**失败分两种**，由接口层翻成不同 HTTP 状态：
- `AiNotConfigured` → 503（`LLM_*` 三项没配齐，属于部署问题）
- `AiFailed` → 502（网络 / 4xx-5xx / 空回复，属于这一次没成）
"""

import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import session_scope
from app.models import (
    BreadthDaily,
    Lhb,
    LimitPool,
    MarketSentiment,
    PatternHit,
    StockBasic,
    StockDaily,
    StockDde,
)
from app.services import llm
from app.services.patterns import PATTERNS
from app.services.stock_phase import load_phase

logger = logging.getLogger(__name__)


class AiNotConfigured(RuntimeError):
    """LLM 三项配置不全 —— 不是用户的问题，别让按钮看起来像「生成失败」。"""


class AiFailed(RuntimeError):
    """这一次没生成出来（没有日线、模型报错、空回复）。"""


#: 形态命中回看的日历天数（约 40 个交易日）。再长就是「这只票历史上中过什么」，
#: 对当天判断没有增量，只是把 prompt 撑长。
PATTERN_WINDOW_DAYS = 60

#: 资金流回看几个交易日。
DDE_DAYS = 5

#: 涨停 / 龙虎榜的统计窗口（日历天）。
TRACE_WINDOW_DAYS = 60

#: 缓存条数上限。值很小（几百字），但要防止反复换代码把内存堆起来。
_CACHE_MAX = 512

_SYSTEM_PROMPT = """你是 A 股个股复盘助手。只能使用用户给出的数字与判读，不得编造新闻、公告、政策、传闻、业绩或资金数据；本站**不提供新闻与公告数据**，凡涉及消息面一律写「未纳入」。
不得给出买入/卖出建议、目标价、止损止盈价位、仓位建议，也不得预测明日涨跌。
请写 4 段纯文本，每段 2~3 句，总字数不超过 450 字；不用 Markdown、不用标题、不用编号、不用表格：
第一段：现在处于什么状态（至少引用给定的两个数字，如涨幅、均线偏离、阶段判定）；
第二段：这个状态背后的逻辑（只依据给定的形态命中、主力净流入、板块与市场情绪）；
第三段：什么情况下这个判断会失效（写可观察的价格或指标条件，不写操作指令）；
第四段：接下来最该盯的一件事（只依据给定数据）。
数字一律沿用给定的口径与单位，不要自己换算或补全。"""


@dataclass(frozen=True, slots=True)
class StockReview:
    """一次研判的结果。`as_of` 是喂给模型的**数据日**（这只票最新一根日线的日期）。"""

    text: str
    as_of: date
    generated_at: datetime


#: (代码, 数据日) → 结果
_CACHE: dict[tuple[str, date], StockReview] = {}
#: 护住 `_CACHE` 与 `_INFLIGHT` 的整段读写（FastAPI 同步接口跑在线程池里）
_CACHE_LOCK = threading.Lock()
#: (代码, 数据日) → 正在生成的那把锁。同一个键并发点时，后来者等前一个，然后读缓存
_INFLIGHT: dict[tuple[str, date], threading.Lock] = {}


def _as_of(session: Session, code: str) -> date | None:
    """这只票最新一根**可用**日线的日期（停牌残行 `pct_chg` 为空，不算）。"""
    return session.scalar(
        select(func.max(StockDaily.trade_date)).where(
            StockDaily.code == code, StockDaily.pct_chg.is_not(None)
        )
    )


def _cache_get(key: tuple[str, date]) -> StockReview | None:
    with _CACHE_LOCK:
        return _CACHE.get(key)


def _cache_put(key: tuple[str, date], review: StockReview) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = review
        if len(_CACHE) > _CACHE_MAX:
            # 只按生成时间丢最旧的一条：键里带数据日，过期条目本身不会再被读到
            oldest = min(_CACHE.items(), key=lambda kv: kv[1].generated_at)
            _CACHE.pop(oldest[0], None)


def _key_lock(key: tuple[str, date]) -> threading.Lock:
    with _CACHE_LOCK:
        lock = _INFLIGHT.get(key)
        if lock is None:
            lock = threading.Lock()
            _INFLIGHT[key] = lock
        return lock


def cached_review(code: str) -> StockReview | None:
    """只读缓存，**不触发**模型调用。给接口层判断「这一下要不要真的花钱」用。"""
    with session_scope() as session:
        as_of = _as_of(session, code)
    if as_of is None:
        return None
    return _cache_get((code, as_of))


def _name(session: Session, code: str) -> str | None:
    basic = session.get(StockBasic, code)
    if basic is not None and basic.name:
        return basic.name
    return session.scalar(
        select(StockDaily.name)
        .where(StockDaily.code == code, StockDaily.name.is_not(None))
        .order_by(StockDaily.trade_date.desc())
        .limit(1)
    )


def _ma(closes: list[float], days: int) -> float | None:
    if len(closes) < days:
        return None
    return sum(closes[:days]) / days


def _money(value: float | None) -> str | None:
    if value is None:
        return None
    return f"{value / 1e8:.2f} 亿元"


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _market_context(session: Session, as_of: date) -> list[str]:
    """最新一个**不晚于** `as_of` 交易日的市场情绪与宽度。带上日期，别让它当成今天。"""
    lines: list[str] = []
    senti_date = session.scalar(
        select(func.max(MarketSentiment.trade_date)).where(
            MarketSentiment.trade_date <= as_of
        )
    )
    if senti_date is not None:
        senti = session.get(MarketSentiment, senti_date)
        parts = []
        if senti is not None:
            if senti.limit_up_count is not None:
                parts.append(f"涨停 {senti.limit_up_count} 家")
            if senti.limit_down_count is not None:
                parts.append(f"跌停 {senti.limit_down_count} 家")
            if senti.broken_rate is not None:
                parts.append(f"炸板率 {senti.broken_rate:.1f}%")
            if senti.max_consecutive is not None:
                parts.append(f"最高 {senti.max_consecutive} 板")
        if parts:
            lines.append(f"市场情绪（{senti_date}）：" + "、".join(parts))

    breadth_date = session.scalar(
        select(func.max(BreadthDaily.trade_date)).where(BreadthDaily.trade_date <= as_of)
    )
    if breadth_date is not None:
        breadth = session.get(BreadthDaily, breadth_date)
        parts = []
        if breadth is not None:
            if breadth.above_ma20 is not None:
                parts.append(f"站上 MA20 {breadth.above_ma20:.1f}%")
            if breadth.above_ma60 is not None:
                parts.append(f"站上 MA60 {breadth.above_ma60:.1f}%")
        if parts:
            lines.append(f"全市场宽度（{breadth_date}）：" + "、".join(parts))
    return lines


def _facts(session: Session, code: str, as_of: date) -> str:
    """给模型看的纯文本事实。缺失的项整行省略，不写「—」。"""
    name = _name(session, code) or code
    rows = list(
        session.scalars(
            select(StockDaily)
            .where(
                StockDaily.code == code,
                StockDaily.pct_chg.is_not(None),
                StockDaily.trade_date <= as_of,
            )
            .order_by(StockDaily.trade_date.desc())
            .limit(60)
        )
    )
    latest = rows[0] if rows else None
    closes = [float(row.close) for row in rows if row.close is not None]
    lines: list[str] = []

    basic = session.get(StockBasic, code)
    industry = basic.industry if basic is not None and basic.industry else None
    head = f"标的：{name}（{code}）"
    if industry:
        head += f"，同花顺行业：{industry}"
    lines.append(f"{head}；数据截至 {as_of}（未复权收盘，涨跌幅为真实涨跌幅）。")

    if latest is not None:
        bits = []
        if latest.close is not None:
            bits.append(f"收盘 {latest.close:.2f}")
        if latest.pct_chg is not None:
            bits.append(f"当日 {latest.pct_chg:+.2f}%")
        if latest.turnover is not None:
            bits.append(f"换手 {latest.turnover:.2f}%")
        amount5 = _avg([row.amount for row in rows[:5] if row.amount is not None])
        if amount5 is not None:
            bits.append(f"近 5 日均成交额 {_money(amount5)}")
        if len(closes) >= 6:
            base = closes[5]
            if base:
                bits.append(f"近 5 日 {(closes[0] / base - 1) * 100:+.2f}%")
        if len(closes) >= 21:
            base = closes[20]
            if base:
                bits.append(f"近 20 日 {(closes[0] / base - 1) * 100:+.2f}%")
        highs = [float(row.high) for row in rows[:20] if row.high is not None]
        if highs and closes:
            high20 = max(highs)
            bits.append(f"20 日最高 {high20:.2f}，当前距其 {(closes[0] / high20 - 1) * 100:+.2f}%")
        if bits:
            lines.append("行情：" + "、".join(bits) + "。")

    mas = [(days, _ma(closes, days)) for days in (5, 10, 20, 60)]
    parts = [f"MA{days} {value:.2f}" for days, value in mas if value is not None]
    if parts:
        text = "均线：" + " / ".join(parts)
        ma20 = _ma(closes, 20)
        if ma20 and closes:
            text += f"；收盘相对 MA20 {(closes[0] / ma20 - 1) * 100:+.2f}%"
        lines.append(text + "。")

    phase = load_phase(session, code)
    if phase is not None:
        text = f"阶段判定：{phase.label}（{phase.phase}）。{phase.text}"
        if phase.plan_text:
            text += f" 计划口径：{phase.plan_text}"
        if phase.risk_text:
            text += f" 风险：{phase.risk_text}"
        if phase.watch_price:
            text += f" 参考观察价：{phase.watch_price:.2f}"
        lines.append(text)
    else:
        lines.append("阶段判定：日线不足 25 根，本站没有给出判定。")

    since = as_of - timedelta(days=PATTERN_WINDOW_DAYS)
    hits = list(
        session.scalars(
            select(PatternHit)
            .where(PatternHit.code == code, PatternHit.trade_date >= since)
            .order_by(PatternHit.trade_date.desc())
        )
    )
    if hits:
        names = {pattern.key: pattern.name for pattern in PATTERNS}
        grouped: dict[str, list[date]] = {}
        for hit in hits:
            # 形态注册表里已经下线的 key（如 weekly_bull）不要原样喂给模型：
            # 那是内部标识，既看不懂又容易被当成新形态，合并成一条更好读
            grouped.setdefault(names.get(hit.pattern, "已下线形态"), []).append(
                hit.trade_date
            )
        parts = [
            f"{label}（{len(days)} 次，最近 {max(days)}）"
            for label, days in sorted(grouped.items(), key=lambda kv: -len(kv[1]))[:8]
        ]
        lines.append(f"近 {PATTERN_WINDOW_DAYS} 天形态命中：" + "、".join(parts) + "。")
    else:
        lines.append(f"近 {PATTERN_WINDOW_DAYS} 天形态命中：无。")

    dde_rows = list(
        session.scalars(
            select(StockDde)
            .where(StockDde.code == code, StockDde.trade_date <= as_of)
            .order_by(StockDde.trade_date.desc())
            .limit(DDE_DAYS)
        )
    )
    if dde_rows:
        known = [row for row in dde_rows if row.net_inflow is not None]
        parts = [f"{row.trade_date} {_money(row.net_inflow)}" for row in known]
        text = f"主力净流入（最近 {len(dde_rows)} 个交易日）"
        if known:
            text += "：合计 " + str(_money(sum(row.net_inflow for row in known)))
        if parts:
            text += "（逐日：" + "、".join(parts) + "）"
        if dde_rows[0].dde is not None:
            text += f"；最新 5 日 DDE {_money(dde_rows[0].dde)}"
        lines.append(text + "。")

    trace_since = as_of - timedelta(days=TRACE_WINDOW_DAYS)
    limit_up = (
        session.scalar(
            select(func.count())
            .select_from(LimitPool)
            .where(
                LimitPool.code == code,
                LimitPool.pool_type == "up",
                LimitPool.trade_date >= trace_since,
            )
        )
        or 0
    )
    lhb = (
        session.scalar(
            select(func.count())
            .select_from(Lhb)
            .where(Lhb.code == code, Lhb.trade_date >= trace_since)
        )
        or 0
    )
    lines.append(f"近 {TRACE_WINDOW_DAYS} 天：涨停 {limit_up} 次，上龙虎榜 {lhb} 次。")

    lines.extend(_market_context(session, as_of))
    return "\n".join(lines)


def review(code: str, settings: Settings | None = None) -> StockReview:
    """生成（或读缓存）一次研判。可能抛 `AiNotConfigured` / `AiFailed`。"""
    settings = settings or get_settings()
    if not llm.configured(settings):
        raise AiNotConfigured("LLM 未配置")

    with session_scope() as session:
        as_of = _as_of(session, code)
    if as_of is None:
        raise AiFailed("这只票还没有可用的日线，无法研判")

    key = (code, as_of)
    hit = _cache_get(key)
    if hit is not None:
        return hit

    with _key_lock(key):
        # 等锁期间别人可能已经生成好了
        hit = _cache_get(key)
        if hit is not None:
            return hit
        with session_scope() as session:
            facts = _facts(session, code, as_of)
        try:
            content = llm.chat(
                settings,
                _SYSTEM_PROMPT,
                f"{facts}\n\n请按系统要求写研判。",
                max_tokens=900,
                temperature=0.4,
            )
        except llm.LlmError as exc:
            logger.warning("个股 AI 研判失败（%s）：%s", code, exc)
            raise AiFailed(str(exc)) from exc

    review_result = StockReview(text=content, as_of=as_of, generated_at=datetime.now())
    _cache_put(key, review_result)
    return review_result
