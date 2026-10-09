"""个股「阶段判定」的取数壳：`code` → `PhaseVerdict`。

判定本身在 `services/patterns.classify_phase`（与形态选股的「明天盯 / 今天可买」
**共用同一批判据**）；这里只管把库里最近 `PHASE_BARS` 根日线取出来、剔掉没法用的行、
转成 `Bars`，然后交给它。

为什么要单独一个模块：**个股页**（`api/stock.profile`）与**个股分析页**
（`api/analysis.lookup`）都要这一段。两边各写一遍早晚会分叉 —— 比如一处取 60 根、
一处取 130 根，`ma60` / `high_120` 那两步就跟着不一样，而症状只是「同一个票两个页面
的左侧压力不一样」，很难往回找。
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import StockDaily
from app.services.patterns import Bars, PhaseVerdict, build_bars, classify_phase

logger = logging.getLogger(__name__)

#: 取多少根日线。`classify_phase` 至少要 25 根，而它里面的 `high_120`（左侧压力）
#: 想看满 120 —— 留一点余量，但别太多（这是一次同步查询，个股页每次都要跑）。
PHASE_BARS = 130


def load_phase(session: Session, code: str) -> PhaseVerdict | None:
    """取最近的日线算阶段判定。根数不够（次新 / 刚缓存）返回 None，调用方不显示这块。"""
    rows = list(
        session.scalars(
            select(StockDaily)
            .where(StockDaily.code == code)
            .order_by(StockDaily.trade_date.desc())
            .limit(PHASE_BARS)
        )
    )
    # 只留能当一根 K 线用的行：iFinD 对停牌日会给一行「只有收盘价、其余全空」的残行，
    # `build_bars` 里 `float(None)` 会直接抛 500（与 `api/stock._adjusted` 同一个口径）。
    usable = [
        item
        for item in reversed(rows)
        if item.close is not None
        and item.pct_chg is not None
        and item.open is not None
        and item.high is not None
        and item.low is not None
    ]
    if not usable:
        return None
    bars: Bars = build_bars(
        [
            {
                "date": item.trade_date,
                "open": item.open,
                "high": item.high,
                "low": item.low,
                "close": item.close,
                "volume": item.volume,
                "amount": item.amount,
                "pct_chg": item.pct_chg,
            }
            for item in usable
        ]
    )
    return classify_phase(bars)
