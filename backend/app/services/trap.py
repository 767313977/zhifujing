"""杀猪盘体检：用**本站已有的结构化数据**做 8 信号排查。

为什么不直接搬 UZI-Skill 的 `fetch_trap_signals.py`：它靠「8 条 web 搜索 × 关键词
计数」，实测两个方向都出错 —— 茅台因为「公司辟谣涨价传闻」被判成风险信号，
而 ST 合力泰（ST + 游资建仓文章 + 严重异动 + 股吧「大霄老师」）反而是 0/8 安全。
本模块改成**先看库里的硬证据，新闻只作辅助**：

| # | 信号 | 数据来源 | 判定 |
| --- | --- | --- | --- |
| 1 | ST / 退市风险 | `stock_basic.name` | 名称含 ST / 退 |
| 2 | 连板 / 短期暴涨 | `limit_pool` + `stock_daily` | 最高 ≥3 连板、60 日涨停 ≥4 次、或 20 日涨幅 ≥50% |
| 3 | 换手异常 | `stock_daily.turnover` | 近 5 日均换手高于 250 日 90 分位，或单日 >25% |
| 4 | 反复上龙虎榜 | `lhb` | 近 90 天上榜 ≥3 天 |
| 5 | 小市值高波动 | `stock_universe.total_mv` + 日线 | 市值 <80 亿且 20 日涨幅 ≥30%（或单日逼近涨停） |
| 6 | 基本面与热度脱节 | — | **数据不足**（本站没有营收/净利润），不投票 |
| 7 | 推广话术 / 澄清 | 东财个股新闻（现取） | 出现「必涨/翻倍/老师带单」等 ≥2 条；有「辟谣/澄清/不实」则**不算风险** |
| 8 | 跨平台 / 社群引流 | — | **数据不足**（需要社媒搜索，本站不接） |

评分只加「命中」信号的权重；`unknown` 不投票、也不当成 0 分。这一点与 UZI-Skill
的原则一致：**没有证据就不装成「安全」**，但也不装成「有风险」。
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import session_scope
from app.models import Lhb, LimitPool, StockBasic, StockDaily, StockUniverse
from app.services import financials
from app.sources.em_news import fetch_stock_news

logger = logging.getLogger(__name__)

# 各信号的权重。总分封顶 100，等级按下面的 LEVELS 切。
WEIGHTS = {
    "st": 35,
    "surge": 20,
    "turnover": 15,
    "lhb": 15,
    "small_cap": 10,
    "news": 10,
}

# 总分 → 等级。与 UZI-Skill 的四档一致（🟢/🟡/🟠/🔴）。
LEVELS = (
    (60, "high", "🔴 高度可疑"),
    (40, "orange", "🟠 警惕"),
    (20, "medium", "🟡 注意"),
    (0, "low", "🟢 安全"),
)

# 龙虎榜 / 涨停池的「数据够不够新」判据：表里最新一天比目标日旧这么多天，
# 就不拿它下结论（采集可能断了，没数据 ≠ 没上榜）。
STALE_DAYS = 10

# 新闻里的两类词：DEBUNK 是公司/媒体辟谣（利好信号），PROMO 是推广话术
DEBUNK_WORDS = ("辟谣", "澄清", "不实")
PROMO_WORDS = (
    "必涨",
    "翻倍",
    "暴涨",
    "稳赚",
    "内幕",
    "老师",
    "股神",
    "跟单",
    "带单",
    "建仓",
    "即将爆发",
)

# 用户补充背景的触发词（与 UZI-Skill「朋友推荐自动升级」同一思路）：
# 强触发 +2 档，普通触发 +1 档；取最大，不叠加。
CONTEXT_STRONG = ("内幕", "稳赚")
CONTEXT_NORMAL = ("朋友推荐", "朋友说", "群里", "老师", "跟单", "带单", "小红书", "抖音")

# 等级从低到高，用于「用户补充背景」上调档位
LEVEL_ORDER = ("low", "medium", "orange", "high")
LEVEL_LABELS = {
    "low": "🟢 安全",
    "medium": "🟡 注意",
    "orange": "🟠 警惕",
    "high": "🔴 高度可疑",
}


@dataclass(slots=True)
class TrapSignal:
    key: str
    name: str
    status: str  # hit / miss / unknown
    severity: str  # high / medium / low
    evidence: str


@dataclass(slots=True)
class TrapResult:
    code: str
    name: str | None
    trade_date: date | None
    score: int
    level: str
    level_label: str
    summary: str
    signals: list[TrapSignal] = field(default_factory=list)
    #: 用户补充背景带来的等级上调档数（0 = 没提供或不含触发词）
    context_boost: int = 0


def _latest_table_day(session: Session, model) -> date | None:
    return session.scalar(select(func.max(model.trade_date)))


def _pct(values: list[float], q: float) -> float | None:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return float(np.percentile(clean, q))


def _check_st(name: str | None) -> TrapSignal:
    text = (name or "").upper()
    if "ST" in text:
        return TrapSignal("st", "ST / 退市风险", "hit", "high", f"当前简称「{name}」，带风险警示")
    if "退" in (name or ""):
        return TrapSignal("st", "ST / 退市风险", "hit", "high", f"当前简称「{name}」，含退市标记")
    return TrapSignal("st", "ST / 退市风险", "miss", "low", f"简称「{name or '—'}」无 ST / 退市标记")


def _check_surge(
    session: Session, code: str, trade_date: date, daily: list[StockDaily]
) -> TrapSignal:
    since = trade_date - timedelta(days=90)
    rows = session.execute(
        select(LimitPool.pool_type, func.count(), func.max(LimitPool.consecutive))
        .where(LimitPool.code == code, LimitPool.trade_date >= since)
        .group_by(LimitPool.pool_type)
    ).all()
    counts = {pool_type: int(count) for pool_type, count, _ in rows}
    max_consecutive = max((int(consecutive or 0) for _, _, consecutive in rows), default=0)

    ret20 = None
    if len(daily) >= 21:
        base = float(daily[-21].close or 0)
        if base > 0:
            ret20 = float(daily[-1].close) / base - 1

    latest_pool_day = _latest_table_day(session, LimitPool)
    stale = latest_pool_day is None or (trade_date - latest_pool_day).days > STALE_DAYS
    evidence = (
        f"近 90 天涨停 {counts.get('up', 0)} 次、炸板 {counts.get('broken', 0)} 次、"
        f"最高 {max_consecutive} 连板"
        + (f"；20 日累计 {ret20 * 100:+.1f}%" if ret20 is not None else "")
    )
    if stale and not counts:
        return TrapSignal("surge", "连板 / 短期暴涨", "unknown", "low", evidence + "；涨停池数据不新鲜")

    hit = max_consecutive >= 3 or counts.get("up", 0) >= 4 or (ret20 is not None and ret20 >= 0.5)
    severity = "high" if max_consecutive >= 3 or (ret20 is not None and ret20 >= 0.5) else "medium"
    return TrapSignal(
        "surge", "连板 / 短期暴涨", "hit" if hit else "miss", severity if hit else "low", evidence
    )


def _check_turnover(daily: list[StockDaily]) -> TrapSignal:
    turns = [float(row.turnover) for row in daily if row.turnover is not None]
    if len(turns) < 60:
        return TrapSignal(
            "turnover", "换手异常", "unknown", "low", f"有效换手率只有 {len(turns)} 天，样本不足"
        )
    recent = turns[-5:]
    base = turns[:-5]
    p90 = _pct(base, 90)
    avg5 = float(np.mean(recent))
    peak = max(recent)
    if p90 is None:
        evidence = f"近 5 日均换手 {avg5:.2f}%、单日最高 {peak:.2f}%"
    else:
        evidence = (
            f"近 5 日均换手 {avg5:.2f}%、单日最高 {peak:.2f}%；"
            f"此前 250 日 90 分位 {p90:.2f}%"
        )
    hit = peak >= 25 or (p90 is not None and avg5 >= p90 and avg5 >= 5)
    return TrapSignal(
        "turnover",
        "换手异常",
        "hit" if hit else "miss",
        "high" if hit and peak >= 25 else ("medium" if hit else "low"),
        evidence,
    )


def _check_lhb(session: Session, code: str, trade_date: date) -> TrapSignal:
    since = trade_date - timedelta(days=90)
    days = int(
        session.scalar(
            select(func.count(func.distinct(Lhb.trade_date))).where(
                Lhb.code == code, Lhb.trade_date >= since
            )
        )
        or 0
    )
    latest = _latest_table_day(session, Lhb)
    stale = latest is None or (trade_date - latest).days > STALE_DAYS
    if days == 0 and stale:
        return TrapSignal("lhb", "反复上龙虎榜", "unknown", "low", "龙虎榜数据不新鲜，无法判断")
    reasons = list(
        session.scalars(
            select(Lhb.reason)
            .where(Lhb.code == code, Lhb.trade_date >= since)
            .order_by(Lhb.trade_date.desc())
            .limit(3)
        )
    )
    evidence = f"近 90 天上榜 {days} 天" + (f"；最近原因：{'、'.join(reasons)}" if reasons else "")
    hit = days >= 3
    return TrapSignal(
        "lhb", "反复上龙虎榜", "hit" if hit else "miss", "high" if days >= 5 else "medium", evidence
    )


def _check_small_cap(
    session: Session, code: str, daily: list[StockDaily]
) -> TrapSignal:
    total_mv = session.scalar(
        select(StockUniverse.total_mv).where(StockUniverse.code == code)
    )
    if total_mv is None:
        total_mv = session.scalar(select(StockBasic.total_mv).where(StockBasic.code == code))
    if total_mv is None or len(daily) < 21:
        return TrapSignal("small_cap", "小市值高波动", "unknown", "low", "缺少市值或日线，无法判断")
    base = float(daily[-21].close or 0)
    ret20 = float(daily[-1].close) / base - 1 if base > 0 else 0.0
    peak = max(
        (float(row.pct_chg) for row in daily[-20:] if row.pct_chg is not None), default=0.0
    )
    cap_yi = float(total_mv) / 1e8
    evidence = f"总市值 {cap_yi:.0f} 亿；20 日累计 {ret20 * 100:+.1f}%、单日最大 {peak:+.1f}%"
    hit = cap_yi < 80 and (ret20 >= 0.3 or peak >= 9.5)
    return TrapSignal(
        "small_cap", "小市值高波动", "hit" if hit else "miss", "medium" if hit else "low", evidence
    )


def _check_news(code: str, name: str | None) -> TrapSignal:
    """东财个股新闻里的推广话术 / 澄清报道。

    ⚠️ 与 UZI-Skill 的差异：**「辟谣」是保护性证据，不是风险** —— 它说明公司/媒体
    已经澄清了传闻，不能和「有人在推这只票」混为一谈。
    """
    try:
        result = fetch_stock_news(code, name=name, limit=30)
    except Exception as exc:  # noqa: BLE001 - 新闻源失败不该拖垮整个体检
        logger.info("%s 的杀猪盘新闻信号取数失败：%s", code, exc)
        return TrapSignal("news", "推广话术 / 澄清", "unknown", "low", "新闻源不可用")
    if not result.items:
        return TrapSignal("news", "推广话术 / 澄清", "miss", "low", "近 30 条新闻里没有相关稿件")

    promo: list[str] = []
    debunk: list[str] = []
    for item in result.items:
        text = f"{item.title} {item.summary}"
        if any(word in text for word in DEBUNK_WORDS):
            debunk.append(item.title)
        elif any(word in text for word in PROMO_WORDS):
            promo.append(item.title)

    if debunk:
        return TrapSignal(
            "news",
            "推广话术 / 澄清",
            "miss",
            "low",
            f"发现 {len(debunk)} 条澄清/辟谣报道（{debunk[0][:28]}…），不按推广计",
        )
    if len(promo) >= 2:
        return TrapSignal(
            "news",
            "推广话术 / 澄清",
            "hit",
            "medium",
            f"发现 {len(promo)} 条推广话术稿件：{'；'.join(title[:24] for title in promo[:2])}",
        )
    return TrapSignal(
        "news",
        "推广话术 / 澄清",
        "miss",
        "low",
        f"推广话术稿件 {len(promo)} 条，未达阈值（≥2 条）",
    )


def _heat(daily: list[StockDaily]) -> tuple[float | None, float | None, float | None]:
    """(20 日累计涨幅, 近 5 日均换手, 此前 250 日换手 90 分位)。"""
    ret20 = None
    if len(daily) >= 21 and daily[-21].close:
        ret20 = float(daily[-1].close) / float(daily[-21].close) - 1

    turns = [float(row.turnover) for row in daily if row.turnover is not None]
    avg5 = float(np.mean(turns[-5:])) if len(turns) >= 5 else None
    p90 = _pct(turns[:-5], 90) if len(turns) >= 60 else None
    return ret20, avg5, p90


def _yoy_text(value: float | None) -> str:
    return "—" if value is None else f"{value:+.1f}%"


def _check_fundamental(
    code: str, trade_date: date, daily: list[StockDaily]
) -> TrapSignal:
    """基本面与热度脱节：**业绩明显走弱** + **股价/换手明显过热**。

    为什么必须加热度条件：只看「净利下滑」会把所有周期股都标成风险；
    「基本面差 + 股价/换手异常热」才是脱节。也正因为如此，它不是「财务差」信号，
    而是「财务差还被炒」信号。
    """
    snapshot = financials.latest(code)
    if snapshot is None:
        return TrapSignal(
            "fundamental", "基本面与热度脱节", "unknown", "low", "财务摘要取不到"
        )
    age = (trade_date - snapshot.report_period).days
    if age > financials.REPORT_MAX_AGE_DAYS:
        return TrapSignal(
            "fundamental",
            "基本面与热度脱节",
            "unknown",
            "low",
            f"最新报告期 {snapshot.report_period} 距今 {age} 天，已过期",
        )

    ret20, avg5, p90 = _heat(daily)
    weak_profit = snapshot.net_profit is not None and snapshot.net_profit < 0
    weak_growth = (
        snapshot.revenue_yoy is not None and snapshot.revenue_yoy <= -20
    ) or (snapshot.net_profit_yoy is not None and snapshot.net_profit_yoy <= -30)
    hot = (ret20 is not None and ret20 >= 0.2) or (
        avg5 is not None and p90 is not None and avg5 >= p90 and avg5 >= 5
    )

    profit_yi = (
        f"{snapshot.net_profit / 1e8:.2f} 亿"
        if snapshot.net_profit is not None
        else "—"
    )
    evidence = (
        f"{snapshot.report_period} 报告期：营收 {_yoy_text(snapshot.revenue_yoy)}、"
        f"净利 {_yoy_text(snapshot.net_profit_yoy)}（{profit_yi}）；"
        f"20 日 {('—' if ret20 is None else f'{ret20 * 100:+.1f}%')}、"
        f"近 5 日均换手 {('—' if avg5 is None else f'{avg5:.2f}%')}"
    )
    hit = (weak_profit or weak_growth) and hot
    high = weak_profit and ret20 is not None and ret20 >= 0.3
    return TrapSignal(
        "fundamental",
        "基本面与热度脱节",
        "hit" if hit else "miss",
        "high" if high else ("medium" if hit else "low"),
        evidence,
    )


def _context_boost(context: str | None) -> tuple[int, str | None]:
    """用户补充背景 → 上调档数。取最大触发，不叠加。"""
    text = (context or "").strip()
    if not text:
        return 0, None
    if any(word in text for word in CONTEXT_STRONG):
        return 2, text
    if any(word in text for word in CONTEXT_NORMAL):
        return 1, text
    return 0, text


def check(code: str, context: str | None = None) -> TrapResult | None:
    """跑完 8 个信号。代码不在 `stock_basic` 里时返回 None。

    `context` 是用户补充的背景（"朋友推荐我" / "群里说的" / "老师带单"）；命中触发词时
    **只上调最终等级、不改评分** —— 评分是数据证据，用户背景是额外的一档判断。
    """
    with session_scope() as session:
        basic = session.get(StockBasic, code)
        if basic is None:
            return None
        name = basic.name
        trade_date = session.scalar(
            select(func.max(StockDaily.trade_date)).where(StockDaily.code == code)
        )
        if trade_date is None:
            return TrapResult(
                code=code,
                name=name,
                trade_date=None,
                score=0,
                level="low",
                level_label="🟢 安全",
                summary="该股没有日线，无法体检（不代表安全）",
                signals=[],
            )
        daily = list(
            session.scalars(
                select(StockDaily)
                .where(StockDaily.code == code, StockDaily.trade_date <= trade_date)
                .order_by(StockDaily.trade_date.desc())
                .limit(260)
            )
        )[::-1]

        signals = [
            _check_st(name),
            _check_surge(session, code, trade_date, daily),
            _check_turnover(daily),
            _check_lhb(session, code, trade_date),
            _check_small_cap(session, code, daily),
        ]

    # 财务摘要与新闻都会各自开连接 / 发请求，放在 session 之外，避免嵌套会话。
    signals.append(_check_fundamental(code, trade_date, daily))
    signals.append(_check_news(code, name))
    signals.append(
        TrapSignal(
            "social",
            "跨平台 / 社群引流",
            "unknown",
            "low",
            "需要社媒搜索，本站未接入",
        )
    )

    boost, context_text = _context_boost(context)
    if boost:
        signals.append(
            TrapSignal(
                "context",
                "用户补充背景",
                "hit",
                "high" if boost >= 2 else "medium",
                f"你提到「{context_text[:40]}」；风险等级上调 {boost} 档",
            )
        )

    score = min(
        100,
        sum(
            WEIGHTS.get(signal.key, 0)
            for signal in signals
            if signal.status == "hit"
        ),
    )
    level, level_label = next(
        (level_name, label)
        for threshold, level_name, label in LEVELS
        if score >= threshold
    )
    if boost:
        index = min(len(LEVEL_ORDER) - 1, LEVEL_ORDER.index(level) + boost)
        level = LEVEL_ORDER[index]
        level_label = LEVEL_LABELS[level]

    hits = [
        signal.name
        for signal in signals
        if signal.status == "hit" and signal.key != "context"
    ]
    unknown = [signal.name for signal in signals if signal.status == "unknown"]
    if hits:
        summary = f"命中 {len(hits)} 个信号：{'、'.join(hits)}"
        if unknown:
            summary += f"（另有 {len(unknown)} 项数据不足，未投票）"
    else:
        summary = "已核对的信号均未命中"
        if unknown:
            summary += f"（{len(unknown)} 项数据不足：{'、'.join(unknown)}）"
    if boost:
        summary += f"；你补充的背景「{context_text[:30]}」使等级上调 {boost} 档"

    return TrapResult(
        code=code,
        name=name,
        trade_date=trade_date,
        score=score,
        level=level,
        level_label=level_label,
        summary=summary,
        signals=signals,
        context_boost=boost,
    )
