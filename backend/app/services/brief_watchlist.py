"""飞书简报里的「自选股异动」一节（2026-10-18 清单 #3）。

只列**当天有异动**的自选股：命中形态、站上/跌破关键位、放量、出公告、
硬证据体检进入高风险档。没有异动的股票不出现 —— 自选页本身能看全量，
简报每天列一遍只会变成噪音。

公告走 `stock_events.load`：**缓存新鲜就读缓存，过期就现取**（2026-10-18 用户要求
「保证每只自选都有」）。公告缓存 6 小时，所以一天里第一次推简报时每只自选最多
打一次东财；之后的推送直接读库。
"""

import logging
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PatternHit, StockBasic, StockDaily, Watchlist
from app.services import stock_events, trade_plan, trap
from app.services.patterns import PATTERNS

logger = logging.getLogger(__name__)

PATTERN_NAMES = {pattern.key: pattern.name for pattern in PATTERNS}
_KEYS = tuple(PATTERN_NAMES)
#: 放量阈值：单日涨幅 / 成交额相对 20 日均值
VOLUME_PCT = 5.0
VOLUME_MULT = 2.0


def _amount(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value / 1e8:.1f} 亿" if abs(value) >= 1e8 else f"{value / 1e4:.0f} 万"


def _levels(key_levels: dict | None) -> tuple[float | None, float | None]:
    data = key_levels or {}
    buy = data.get("breakout") or data.get("watch_high")
    stop = data.get("support") or data.get("wash_low") or data.get("start_low")
    return (
        float(buy) if buy else None,
        float(stop) if stop else None,
    )


def _recent_notice(code: str, trade_date: date) -> str | None:
    """最近一条**当天或前一天**的公告；没有就返回 None。

    用 `load` 而不是 `peek`：缓存过期时会现取，保证每只自选都有公告可看。
    """
    floor = (trade_date - timedelta(days=1)).isoformat()
    for row in stock_events.load(code, "notices")["rows"]:
        if str(row.get("published_at") or "") < floor:
            continue
        title = str(row.get("title") or "")
        if not title:
            continue
        risky = any(word in title for word in trap.RISK_NOTICE_WORDS)
        prefix = "⚠ 风险公告：" if risky else "公告："
        return f"{prefix}{title[:28]}（{row.get('published_at')}）"
    return None


def block(session: Session, trade_date: date) -> str:
    user_id = trade_plan.admin_user_id(session)
    if user_id is None:
        return ""
    codes = list(
        session.scalars(select(Watchlist.code).where(Watchlist.user_id == user_id))
    )
    if not codes:
        return ""

    # 当天命中的最高分形态（一只票多个形态时取分最高的那个）
    hit_rows = session.execute(
        select(
            PatternHit.code,
            PatternHit.name,
            PatternHit.pattern,
            PatternHit.score,
            PatternHit.key_levels,
        )
        .where(
            PatternHit.trade_date == trade_date,
            PatternHit.code.in_(codes),
            PatternHit.pattern.in_(_KEYS),
        )
        .order_by(PatternHit.score.desc())
    ).all()
    best_hit: dict[str, tuple] = {}
    for row in hit_rows:
        best_hit.setdefault(row[0], row)

    names = dict(
        session.execute(
            select(StockBasic.code, StockBasic.name).where(StockBasic.code.in_(codes))
        ).all()
    )

    bars = {
        row[0]: row[1:]
        for row in session.execute(
            select(
                StockDaily.code,
                StockDaily.close,
                StockDaily.pct_chg,
                StockDaily.amount,
            ).where(
                StockDaily.trade_date == trade_date,
                StockDaily.code.in_(codes),
            )
        ).all()
    }

    # 20 日均额：只取今天之前 45 个自然日，按日排序后取最近 20 个有值的
    history: dict[str, list[float]] = defaultdict(list)
    for code, amount in session.execute(
        select(StockDaily.code, StockDaily.amount)
        .where(
            StockDaily.code.in_(codes),
            StockDaily.trade_date < trade_date,
            StockDaily.trade_date >= trade_date - timedelta(days=45),
            StockDaily.amount.is_not(None),
        )
        .order_by(StockDaily.code, StockDaily.trade_date)
    ).all():
        history[code].append(float(amount))

    lines: list[str] = []
    for code in codes:
        parts: list[str] = []
        hit = best_hit.get(code)
        bar = bars.get(code)
        name = (hit[1] if hit else None) or names.get(code) or code

        if hit is not None:
            parts.append(f"命中「{PATTERN_NAMES.get(hit[2], hit[2])}」")

        if bar is not None:
            close, pct_chg, amount = bar
            buy, stop = _levels(hit[4] if hit else None)
            if buy is not None and close >= buy:
                parts.append(f"站上突破位 {buy:.2f}")
            if stop is not None and close < stop:
                parts.append(f"跌破支撑 {stop:.2f}")
            avg = history.get(code) or []
            base = sum(avg[-20:]) / len(avg[-20:]) if avg else None
            if pct_chg is not None and pct_chg >= VOLUME_PCT:
                parts.append(f"放量 {pct_chg:+.1f}%")
            elif base and amount is not None and amount >= base * VOLUME_MULT:
                parts.append(f"成交额 {_amount(amount)}（20 日均 {_amount(base)}）")

        notice = _recent_notice(code, trade_date)
        if notice:
            parts.append(notice)

        risk = trap.check(code, fast=True)
        if risk is not None and risk.level == "high":
            parts.append(f"硬证据体检高风险（{risk.score} 分）")

        if parts:
            lines.append(f"- {code} {name}：{'；'.join(parts)}")

    if not lines:
        return ""
    return "## 自选股异动\n" + "\n".join(lines)
