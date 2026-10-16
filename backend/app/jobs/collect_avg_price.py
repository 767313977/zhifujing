"""平均股价（等权，**自算**）—— 全市场个股收盘价的算术平均（元）。

口径（与通达信的 880003「平均股价」同一思路）：
- 当日 `stock_daily` 里收盘价有效、且当天正常交易（`pct_chg` 非空）的**全部个股**，
  收盘价取算术平均 —— 每只票权重相同，所以不会被大市值权重股绑架（那正是上证的问题）；
- `pct_chg` 记成**均值的日变化率**，让 `close` 与 `pct_chg` 自洽。

⚠️ 这条序列是**自算的，不是采来的**：iFinD 没有这个指数代码（2026-10-16 实测
`880003` / `880003.SH` / `880003.BJ` 三种写法都不认）。落进 `index_daily` 是为了让
首页指数卡片、情绪周期页「指数走势」、背离判断**自动复用**现有链路 —— 这是 B 档
第 4 项「指数家族扩容」里唯一要自建的那个。

⚠️ 两点容易误读，前端口径说明里同样写明：
1. **它是「元」，不是点位** —— 均值随股价结构漂移，绝对值别拿去和上证/沪深300 比；
2. 它的日变化率数学上是**价格加权**的涨幅（`Σ p_{t-1}(1+r) / Σ p_{t-1}`），
   比等权收益波动大（实测极端日两者差 3.6 个百分点）。这是「平均股价」这个指标的
   固有性质，不是 bug。

自愈策略与恐贪 / 宽度一致：`refresh()` 重算最近 `REFRESH_DAYS` 个交易日；库里还没有
这条序列时自动退回全量回补（新部署第一趟就自带整段历史）。

骨架用 `stock_daily` 自己的交易日（不是 `market_sentiment`）—— 它有更长的历史
（约 510 天，回溯到 2024-09-02），用情绪表当骨架会把这条序列白白截掉一年。
"""

import logging
from datetime import date

from sqlalchemy import func, select

from app.db import session_scope, upsert_many
from app.models import IndexDaily, StockDaily

logger = logging.getLogger(__name__)

# 自算序列落在 index_daily 里用的代码与名称。沿用通达信的 880003，便于对得上号。
CODE = "880003"
NAME = "平均股价（等权）"

# 每次采集重算最近这么多个交易日（订正 / 漏一天下次自动补，与恐贪 / 宽度同策略）
REFRESH_DAYS = 30

# 「当天正常交易」的判据：收盘价有效为正、且涨跌幅非空。
# 与宽度 / 行业宽度用的那一批有效行同一套，保证几个指标口径一致。
_ACTIVE_FILTERS = (
    StockDaily.close.is_not(None),
    StockDaily.close > 0,
    StockDaily.pct_chg.is_not(None),
)


def _daily_avg(start: date, end: date) -> list[tuple[date, float]]:
    """`[start, end]` 内每个交易日的 (日期, 全市场平均收盘价)。"""
    with session_scope() as session:
        rows = session.execute(
            select(StockDaily.trade_date, func.avg(StockDaily.close))
            .where(StockDaily.trade_date >= start, StockDaily.trade_date <= end, *_ACTIVE_FILTERS)
            .group_by(StockDaily.trade_date)
            .order_by(StockDaily.trade_date)
        ).all()
    return [(day, float(value)) for day, value in rows if value is not None]


def _seed(session, start: date) -> float | None:
    """`start` 之前最后一个有效交易日的平均收盘价，用来给区间首日算 `pct_chg`。

    没有它的话每次 `refresh` 都会把区间首日的涨跌幅写成 None，把上一次算好的值抹掉。
    """
    day = session.scalar(
        select(func.max(StockDaily.trade_date)).where(
            StockDaily.trade_date < start, *_ACTIVE_FILTERS
        )
    )
    if day is None:
        return None
    value = session.scalar(
        select(func.avg(StockDaily.close)).where(StockDaily.trade_date == day, *_ACTIVE_FILTERS)
    )
    return None if value is None else float(value)


def compute_range(start: date, end: date) -> list[dict]:
    """算 `[start, end]` 内每个交易日的平均股价，返回可直接写库的 `IndexDaily` 行。"""
    rows = _daily_avg(start, end)
    if not rows:
        return []

    with session_scope() as session:
        prev: float | None = _seed(session, rows[0][0])

    out: list[dict] = []
    for day, value in rows:
        avg_close = round(value, 4)
        out.append(
            {
                "trade_date": day,
                "code": CODE,
                "name": NAME,
                "close": avg_close,
                "pct_chg": None if prev is None else round((avg_close / prev - 1) * 100, 4),
                # 成交额与涨跌家数**留空**：那几列是「指数口径」，把全市场合计填进去
                # 语义不对（虽然 AMOUNT 汇总只取上证 + 深证，不会真的被用到）。
                # 宁可空着，也不填一个会被误读的数。
                "amount": None,
                "up_count": None,
                "down_count": None,
                "limit_up_count": None,
                "limit_down_count": None,
            }
        )
        prev = avg_close
    return out


def collect_range(start: date, end: date) -> int:
    """算 `[start, end]` 并写库（幂等 upsert），返回写入行数。"""
    rows = compute_range(start, end)
    if not rows:
        return 0
    with session_scope() as session:
        upsert_many(session, IndexDaily, rows)
    return len(rows)


def latest_trade_date() -> date | None:
    with session_scope() as session:
        return session.scalar(select(func.max(StockDaily.trade_date)))


def _has_rows() -> bool:
    with session_scope() as session:
        return bool(
            session.scalar(
                select(func.count()).select_from(IndexDaily).where(IndexDaily.code == CODE)
            )
        )


def backfill() -> int:
    """从 `stock_daily` 的最早交易日算到最新（首日没有前值，`pct_chg` 记 None）。"""
    with session_scope() as session:
        start = session.scalar(select(func.min(StockDaily.trade_date)))
        end = session.scalar(select(func.max(StockDaily.trade_date)))
    if start is None or end is None:
        logger.warning("个股日线表为空，无法回补平均股价")
        return 0
    written = collect_range(start, end)
    logger.info("平均股价回补 %s ~ %s：写入 %d 天", start, end, written)
    return written


def refresh(days: int = REFRESH_DAYS) -> int:
    """重算最近 `days` 个交易日；空表时自动退回全量回补（新部署第一趟自带历史）。"""
    if not _has_rows():
        return backfill()

    end = latest_trade_date()
    if end is None:
        return 0
    with session_scope() as session:
        recent = list(
            session.scalars(
                select(StockDaily.trade_date)
                .where(StockDaily.trade_date <= end)
                .distinct()
                .order_by(StockDaily.trade_date.desc())
                .limit(days)
            )
        )
    if not recent:
        return 0
    return collect_range(min(recent), end)
