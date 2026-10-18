"""历史相似日：用当天的情绪向量找最像的几天，看它们之后怎么走（清单 #4）。

向量（五维，全部来自库里的现成数据）：
- 涨停数、炸板率、最高连板（`market_sentiment`）
- 站上 MA20 的个股占比（`breadth_daily`）
- 恐贪指数（`fear_greed_daily` 的原始值 → `services.fear_greed` 现算）

**用分位排名而不是原始值**：五维量纲差得远（涨停 0~100、炸板率 0~50、恐贪 0~100），
直接算欧氏距离会被数值大的维度主导。每维先映射成「在历史上排多高」（0~1），
再算距离，五维权重相等。

相似度只用于**参照**：它不是预测，历史上像的日子也可能第二天就走反。
全部本地计算，零 iFinD 配额。
"""

import logging
import math
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    BreadthDaily,
    FearGreedDaily,
    IndexDaily,
    LimitPool,
    MarketSentiment,
    TradeCalendar,
)
from app.services import fear_greed, relay_temp

logger = logging.getLogger(__name__)

#: 相似度只看最近这么多交易日（太久以前的市场结构不同，分位也没意义）
LOOKBACK_DAYS = 600
#: 与「今天」相隔这么近的日子不参与（否则最近几天几乎必然排前面，参考价值低）
EXCLUDE_NEAR = 3
#: 基准指数：与站内其余页面一致，用上证指数
BENCHMARK = "000001.SH"
#: 至少要有这么多维都有值才算一个可比的日子
MIN_DIMS = 3

DIMS: tuple[tuple[str, str], ...] = (
    ("limit_up", "涨停数"),
    ("broken_rate", "炸板率"),
    ("max_consecutive", "最高板"),
    ("above_ma20", "站上 MA20"),
    ("fear_greed", "恐贪"),
)


def _rank_map(pairs: list[tuple[date, float]]) -> dict[date, float]:
    """`{日期: 分位排名 0~1}`（并列取平均名次）。"""
    values = [value for _, value in pairs]
    size = len(values)
    if not size:
        return {}
    result: dict[date, float] = {}
    for day, value in pairs:
        below = sum(1 for item in values if item < value)
        equal = sum(1 for item in values if item == value)
        result[day] = (below + equal / 2) / size
    return result


def _relay_series(session: Session) -> dict[date, float | None]:
    rows = list(
        session.scalars(select(MarketSentiment).order_by(MarketSentiment.trade_date))
    )
    if not rows:
        return {}
    up_pool: dict[date, set[str]] = {}
    first_board: dict[date, set[str]] = {}
    for day, code, consecutive in session.execute(
        select(LimitPool.trade_date, LimitPool.code, LimitPool.consecutive)
    ).all():
        up_pool.setdefault(day, set()).add(code)
        if consecutive == 1:
            first_board.setdefault(day, set()).add(code)
    series = relay_temp.build_series(rows, up_pool, first_board)
    return {item["trade_date"]: item["score"] for item in series}


def _fear_series(session: Session) -> dict[date, float | None]:
    rows = list(
        session.scalars(
            select(FearGreedDaily).order_by(FearGreedDaily.trade_date)
        )
    )
    if not rows:
        return {}
    return {
        item["trade_date"]: item["score"] for item in fear_greed.build_series(rows)
    }


def _index_closes(session: Session) -> dict[date, float]:
    return {
        day: float(close)
        for day, close in session.execute(
            select(IndexDaily.trade_date, IndexDaily.close).where(
                IndexDaily.code == BENCHMARK, IndexDaily.close.is_not(None)
            )
        ).all()
    }


def _forward(
    day: date,
    calendar: list[date],
    pos: dict[date, int],
    index_close: dict[date, float],
    relay: dict[date, float | None],
) -> dict:
    """相似日之后 1/3/5 个交易日的指数收益与情绪分数。"""
    start = pos.get(day)
    base = index_close.get(day)
    out: dict = {}
    for horizon in (1, 3, 5):
        target = calendar[start + horizon] if start is not None and start + horizon < len(calendar) else None
        pct = None
        if target is not None and base:
            close = index_close.get(target)
            if close is not None:
                pct = round((close / base - 1) * 100, 2)
        out[f"index_{horizon}d"] = pct
        out[f"sentiment_{horizon}d"] = relay.get(target) if target else None
    return out


def similar(session: Session, target: date | None = None, top: int = 10) -> dict:
    """以 `target`（默认库里的最新情绪日）为「今天」，找最像的 `top` 天。"""
    sentiment = list(
        session.scalars(select(MarketSentiment).order_by(MarketSentiment.trade_date))
    )
    if not sentiment:
        return {"trade_date": None, "vector": [], "rows": [], "lookback_days": 0}

    days = [row.trade_date for row in sentiment]
    if target is None:
        target = days[-1]
    else:
        # 传入非交易日 / 没有情绪数据的日子：退到它之前最近的一个情绪日
        available = [day for day in days if day <= target]
        if not available:
            return {"trade_date": None, "vector": [], "rows": [], "lookback_days": 0}
        target = available[-1]

    start = max(0, len(days) - LOOKBACK_DAYS)
    window = days[start:]
    by_day = {row.trade_date: row for row in sentiment}

    breadth = {
        row.trade_date: row
        for row in session.scalars(
            select(BreadthDaily).where(BreadthDaily.trade_date >= window[0])
        )
    }
    fear = _fear_series(session)
    relay = _relay_series(session)

    calendar = list(
        session.scalars(select(TradeCalendar.trade_date).order_by(TradeCalendar.trade_date))
    )
    pos = {day: index for index, day in enumerate(calendar)}
    index_close = _index_closes(session)

    def raw(day: date, key: str) -> float | None:
        if key == "limit_up":
            return by_day[day].limit_up_count
        if key == "broken_rate":
            return by_day[day].broken_rate
        if key == "max_consecutive":
            return by_day[day].max_consecutive
        if key == "above_ma20":
            row = breadth.get(day)
            return row.above_ma20 if row is not None else None
        if key == "fear_greed":
            return fear.get(day)
        return None

    # 每一维在「截至 target」的窗口里算分位
    limited = [day for day in window if day <= target]
    ranks: dict[str, dict[date, float]] = {}
    for key, _label in DIMS:
        pairs = [(day, value) for day in limited if (value := raw(day, key)) is not None]
        ranks[key] = _rank_map(pairs)

    vector = []
    for key, label in DIMS:
        value = raw(target, key)
        vector.append(
            {
                "key": key,
                "label": label,
                "value": round(value, 2) if value is not None else None,
                "rank": round(ranks[key].get(target, 0.0), 3)
                if target in ranks[key]
                else None,
            }
        )

    target_pos = pos.get(target)
    rows = []
    for day in limited:
        if day == target:
            continue
        if target_pos is not None and day in pos and abs(pos[day] - target_pos) <= EXCLUDE_NEAR:
            continue
        values: list[float] = []
        for key, _label in DIMS:
            left = ranks[key].get(target)
            right = ranks[key].get(day)
            if left is not None and right is not None:
                values.append((left - right) ** 2)
        if len(values) < MIN_DIMS:
            continue
        distance = math.sqrt(sum(values) / len(values))
        rows.append(
            {
                "trade_date": day,
                "distance": round(distance, 4),
                "similarity": round((1 - distance) * 100, 1),
                **{key: raw(day, key) for key, _label in DIMS},
                "relay_score": relay.get(day),
                **_forward(day, calendar, pos, index_close, relay),
            }
        )

    rows.sort(key=lambda item: item["distance"])
    return {
        "trade_date": target,
        "vector": vector,
        "rows": rows[:top],
        "lookback_days": len(limited),
    }
