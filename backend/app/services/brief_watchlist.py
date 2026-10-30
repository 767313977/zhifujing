"""飞书简报里的「自选股异动」一节（2026-10-18 清单 #3）。

只列**当天有异动**的自选股：命中形态、站上/跌破关键位、放量、出公告、
硬证据体检进入高风险档。没有异动的股票不出现 —— 自选页本身能看全量，
简报每天列一遍只会变成噪音。

公告走 `stock_events.load`：**缓存新鲜就读缓存，过期就现取**（2026-10-18 用户要求
「保证每只自选都有」）。公告缓存 6 小时，所以一天里第一次推简报时每只自选最多
打一次东财；之后的推送直接读库。
"""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import CbBasic, CbDaily, PatternHit, StockBasic, StockDaily, Watchlist
from app.services import stock_events, trade_plan, trap
from app.services.convertible import redeem_kind
from app.services.patterns import PATTERNS
from app.services.watch_scope import stock_watch_codes

logger = logging.getLogger(__name__)

PATTERN_NAMES = {pattern.key: pattern.name for pattern in PATTERNS}
_KEYS = tuple(PATTERN_NAMES)
#: 放量阈值：单日涨幅 / 成交额相对 20 日均值
VOLUME_PCT = 5.0
VOLUME_MULT = 2.0
#: 公告现取的总时间预算（秒）。缓存过期时每只自选串行现取，单只最坏 60s
#: （`_call_bounded` 超时）—— 不设总预算的话，N 只自选最坏 N×60s，整节会把
#: 简报链路拖死。超预算的票不再等公告，末尾统一标注「公告未取全」。
NOTICE_BUDGET_SEC = 120.0


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
    # 只算股票：转债代码在 `stock_daily` 里没有行，拿它查公告/形态纯属白花调用
    codes = stock_watch_codes(session, user_id)
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
    deadline = time.monotonic() + NOTICE_BUDGET_SEC
    notice_cut = False
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

        notice = None
        if time.monotonic() < deadline:
            notice = _recent_notice(code, trade_date)
        else:
            notice_cut = True
        if notice:
            parts.append(notice)

        risk = trap.check(code, fast=True)
        if risk is not None and risk.level == "high":
            parts.append(f"硬证据体检高风险（{risk.score} 分）")

        if parts:
            lines.append(f"- {code} {name}：{'；'.join(parts)}")

    if notice_cut:
        lines.append("- （公告取数超出时间预算，部分自选的公告未覆盖）")
    if not lines:
        return ""
    return "**自选股异动**\n" + "\n".join(lines)


@dataclass(frozen=True, slots=True)
class RedeemAlert:
    """一条「自选里的转债已公告强赎」提醒。"""

    bond_code: str
    bond_name: str | None
    stock_code: str | None
    stock_name: str | None
    last_trade_date: date | None
    #: 距最后交易日的自然日数（负数 = 已过）
    days_left: int | None

    def line(self) -> str:
        name = self.bond_name or self.bond_code
        stock = ""
        if self.stock_code:
            label = (
                f"{self.stock_code} {self.stock_name}"
                if self.stock_name
                else self.stock_code
            )
            stock = f"（正股 {label}）"
        text = f"- {self.bond_code} {name}{stock}：**已公告强赎**"
        if self.last_trade_date:
            text += f"，最后交易日 {self.last_trade_date}"
            text += (
                f"（还剩 {self.days_left} 天）"
                if self.days_left is not None and self.days_left >= 0
                else "（已过）"
            )
        return text + "；没转股会按约 100 元赎回"


def redeem_alerts(session: Session, trade_date: date) -> list[RedeemAlert]:
    """自选（或其正股）里**当前处于「已公告强赎」**的转债。

    为什么值得单独推一条：公告之后转债按约 100 元被赎回，没转股的持仓几天里就亏掉
    20%~30%。它是**踩坑提醒**，比「今天买什么」重要，所以不能混在 15:05 的简报里 ——
    简报推送时当天的转债快照（17:30 才采）还没入库，这一节永远是空的（2026-10-30
    复查 P1-2）。现在由 `run_late` 采完转债之后单独推送，去重按**券**维度记
    （见 `push_brief.push_cb_redeem`）：同一只券只提醒一次，哪天采集失败也不会漏。

    「已公告强赎」用 `redeem_kind` 判，别用字符串包含判断 —— 「已满足强赎条件」里
    也有「强赎」两个字。
    """
    user_id = trade_plan.admin_user_id(session)
    if user_id is None:
        return []
    day = session.scalar(select(func.max(CbDaily.trade_date)))
    if day is None or day < trade_date:
        # 当天的转债快照还没采到：拿昨天的状态冒充「今天的提醒」比不提醒更糟
        return []
    codes = list(
        session.scalars(select(Watchlist.code).where(Watchlist.user_id == user_id))
    )
    if not codes:
        return []
    # 自选里可能直接放转债代码（可转债-5），也可能放的是正股 —— 两种都要
    basics = list(
        session.scalars(
            select(CbBasic).where(
                or_(CbBasic.bond_code.in_(codes), CbBasic.stock_code.in_(codes))
            )
        )
    )
    if not basics:
        return []
    dailies = {
        row.bond_code: row
        for row in session.scalars(
            select(CbDaily).where(
                CbDaily.trade_date == day,
                CbDaily.bond_code.in_([row.bond_code for row in basics]),
            )
        )
    }
    alerts: list[RedeemAlert] = []
    for basic in sorted(basics, key=lambda row: row.bond_code):
        daily = dailies.get(basic.bond_code)
        if daily is None or redeem_kind(daily.redeem_status) != "announced":
            continue
        alerts.append(
            RedeemAlert(
                bond_code=basic.bond_code,
                bond_name=basic.bond_name,
                stock_code=basic.stock_code,
                stock_name=basic.stock_name,
                last_trade_date=daily.last_trade_date,
                days_left=(daily.last_trade_date - trade_date).days
                if daily.last_trade_date
                else None,
            )
        )
    return alerts
