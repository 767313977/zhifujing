"""风控与仓位约束提醒（FEAT-RISK-GUARD，2026-10-31）。

**只是提醒，不阻止任何操作**（`strict_mode` 也只在页面上加强提示）。
数据全部读本地库：情绪热度来自 `market_sentiment` 与恐贪指数，集中度来自用户自己
填的持仓仓位百分比（没有填就跳过，不猜 —— 但会在提示里写明跳过，避免被理解成
「没告警 = 不集中」）。

口径：
- 过热：连板高度 ≥ `heat_consecutive`（默认 6）**或** 恐贪指数 ≥ `heat_temperature`
  （默认 80，直接读 `fear_greed` 的最新综合分）；
- 单票集中度：未平仓持仓里同一代码的 `position_pct` 之和 > `max_position_pct`；
- 单题材集中度：按 `stock_concept`（最近一天）把持仓归到题材，同一题材合计
  > `max_theme_pct`。
"""

from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    FearGreedDaily,
    MarketSentiment,
    StockConcept,
    TradeJournal,
    UserRiskPref,
)
from app.services import fear_greed

DEFAULTS: dict[str, Any] = {
    "max_position_pct": 30.0,
    "max_theme_pct": 50.0,
    "heat_consecutive": 6,
    "heat_temperature": 80,
    "strict_mode": False,
}


def get_pref(session: Session, user_id: int) -> dict[str, Any]:
    row = session.get(UserRiskPref, user_id)
    if row is None:
        return {**DEFAULTS}
    return {
        "max_position_pct": row.max_position_pct,
        "max_theme_pct": row.max_theme_pct,
        "heat_consecutive": row.heat_consecutive,
        "heat_temperature": row.heat_temperature,
        "strict_mode": bool(row.strict_mode),
    }


def save_pref(session: Session, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    row = session.get(UserRiskPref, user_id)
    if row is None:
        row = UserRiskPref(user_id=user_id)
        session.add(row)
    row.max_position_pct = payload.get("max_position_pct")
    row.max_theme_pct = payload.get("max_theme_pct")
    row.heat_consecutive = int(payload.get("heat_consecutive") or DEFAULTS["heat_consecutive"])
    row.heat_temperature = int(payload.get("heat_temperature") or DEFAULTS["heat_temperature"])
    row.strict_mode = bool(payload.get("strict_mode"))
    session.commit()
    return get_pref(session, user_id)


def _fear_score(session: Session) -> tuple[float | None, Any]:
    rows = list(
        session.scalars(select(FearGreedDaily).order_by(FearGreedDaily.trade_date))
    )
    latest = fear_greed.latest(fear_greed.build_series(rows))
    if latest is None:
        return None, None
    return latest.get("score"), latest.get("trade_date")


def _concentrations(session: Session, user_id: int, pref: dict[str, Any]) -> list[dict]:
    journals = list(
        session.scalars(
            select(TradeJournal).where(
                TradeJournal.user_id == user_id,
                TradeJournal.exit_date.is_(None),
                TradeJournal.position_pct.is_not(None),
            )
        )
    )
    if not journals:
        return []
    by_code: dict[str, float] = defaultdict(float)
    names: dict[str, str | None] = {}
    for journal in journals:
        by_code[journal.code] += journal.position_pct or 0
        names[journal.code] = journal.name

    items: list[dict] = []
    limit = pref.get("max_position_pct")
    if limit is not None:
        for code, pct in by_code.items():
            if pct > limit:
                items.append(
                    {
                        "kind": "position",
                        "name": names.get(code) or code,
                        "code": code,
                        "pct": round(pct, 1),
                        "limit": limit,
                        "text": f"{names.get(code) or code} 仓位 {pct:.1f}% > 单票上限 {limit:.1f}%",
                    }
                )

    theme_limit = pref.get("max_theme_pct")
    if theme_limit is None:
        return items
    latest_day = session.scalar(select(func.max(StockConcept.trade_date)))
    if latest_day is None:
        return items
    concepts = session.execute(
        select(StockConcept.code, StockConcept.concept).where(
            StockConcept.trade_date == latest_day,
            StockConcept.code.in_(list(by_code)),
        )
    ).all()
    by_theme: dict[str, float] = defaultdict(float)
    for code, concept in concepts:
        by_theme[concept] += by_code.get(code, 0)
    for concept, pct in by_theme.items():
        if pct > theme_limit:
            items.append(
                {
                    "kind": "theme",
                    "name": concept,
                    "code": None,
                    "pct": round(pct, 1),
                    "limit": theme_limit,
                    "text": f"题材「{concept}」合计仓位 {pct:.1f}% > 单题材上限 {theme_limit:.1f}%",
                }
            )
    return items


def status(session: Session, user_id: int) -> dict[str, Any]:
    pref = get_pref(session, user_id)
    triggers: list[str] = []

    sentiment = session.scalar(
        select(MarketSentiment).order_by(MarketSentiment.trade_date.desc()).limit(1)
    )
    if (
        sentiment is not None
        and sentiment.max_consecutive is not None
        and pref["heat_consecutive"] is not None
        and sentiment.max_consecutive >= pref["heat_consecutive"]
    ):
        triggers.append(
            f"连板高度 {sentiment.max_consecutive} ≥ {pref['heat_consecutive']}"
        )

    fear_value, fear_date = _fear_score(session)
    if (
        fear_value is not None
        and pref["heat_temperature"] is not None
        and fear_value >= pref["heat_temperature"]
    ):
        triggers.append(
            f"恐贪指数 {fear_value:.0f} ≥ {pref['heat_temperature']}"
            + (f"（{fear_date}）" if fear_date else "")
        )

    concentrations = _concentrations(session, user_id, pref)
    overheat = bool(triggers)
    parts = triggers + [item["text"] for item in concentrations]

    # 未填仓位比例的持仓**不参与集中度计算**（见模块说明）。必须写出来 ——
    # 否则用户会以为「没告警 = 持仓不集中」，而实际是「这几笔根本没算」。
    missing_pct = (
        session.scalar(
            select(func.count())
            .select_from(TradeJournal)
            .where(
                TradeJournal.user_id == user_id,
                TradeJournal.exit_date.is_(None),
                TradeJournal.position_pct.is_(None),
            )
        )
        or 0
    )
    note = (
        f"{missing_pct} 笔持仓未填仓位比例，不参与集中度计算"
        "（交易计划没有仓位字段，也不计入集中度）"
        if missing_pct
        else ""
    )
    if parts:
        message = "；".join(parts + ([note] if note else []))
    elif note:
        message = f"当前没有触发风控提醒（{note}）"
    else:
        message = "当前没有触发风控提醒"
    return {
        "overheat": overheat,
        "strict_mode": pref["strict_mode"],
        "triggers": triggers,
        "concentration": concentrations,
        "message": message,
        "pref": pref,
    }
