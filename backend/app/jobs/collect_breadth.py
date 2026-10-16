"""市场宽度温度计采集（**零外部请求、零 iFinD 配额**）。

全部从站内 `stock_daily` 反算：
- 站上 MA20 / MA60 / MA250 的个股占比 —— 中期多空格局与牛熊分界
- 创 60 / 250 日新高、新低家数 —— 行情有没有广度支撑

实现是**一条窗口函数 SQL**（见 `_BREADTH_SQL`）：上百万行个股日线若拉进 Python
再滚动，日常尾部链每趟要多花十几秒、峰值内存也压不住；SQLite 3.25+ 的窗口函数
在库里扫一遍只要几秒。

两个口径决定（都在前端口径说明里写明）：
1. 用**不复权收盘价** —— 与行情软件里均线的默认口径一致；除权日会造成一次性下穿，
   影响只落在当天那批除权票上，不做复权重建（那需要把全市场 K 线拉进内存）。
2. 不满 N 根 K 线的票**不进该档分母**：新股上市第 5 天「站上 250 日线」没有意义。
   所以建库初期 `above_ma250` / 250 日新高新低会是 NULL，随 `stock_daily` 历史
   回补自然长出来（恐贪那条只取当日截面，没有这个预热问题）。

日历骨架取 `market_sentiment.trade_date`，与恐贪指数同口径。
"""

import logging
from datetime import date, timedelta

from sqlalchemy import func, select, text

from app.db import session_scope, upsert_many
from app.models import BreadthDaily, MarketSentiment

logger = logging.getLogger(__name__)

# 回补默认的回看自然日数，与 `stock_daily` 的历史深度对齐
DEFAULT_BACKFILL_DAYS = 380

# 每次采集重算最近这么多个交易日（订正 / 漏一天下次自动补，与恐贪同策略）
REFRESH_DAYS = 30

# MA250 需要 250 个交易日的预热 ≈ 365 个自然日，多取 55 天余量
WARMUP_DAYS = 420

_BREADTH_SQL = text(
    """
    WITH bars AS (
        SELECT
            trade_date,
            close,
            ROW_NUMBER() OVER (
                PARTITION BY code ORDER BY trade_date
            ) AS rn,
            AVG(close) OVER (
                PARTITION BY code ORDER BY trade_date
                ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
            ) AS ma20,
            AVG(close) OVER (
                PARTITION BY code ORDER BY trade_date
                ROWS BETWEEN 59 PRECEDING AND CURRENT ROW
            ) AS ma60,
            AVG(close) OVER (
                PARTITION BY code ORDER BY trade_date
                ROWS BETWEEN 249 PRECEDING AND CURRENT ROW
            ) AS ma250,
            MAX(close) OVER (
                PARTITION BY code ORDER BY trade_date
                ROWS BETWEEN 59 PRECEDING AND CURRENT ROW
            ) AS hi60,
            MIN(close) OVER (
                PARTITION BY code ORDER BY trade_date
                ROWS BETWEEN 59 PRECEDING AND CURRENT ROW
            ) AS lo60,
            MAX(close) OVER (
                PARTITION BY code ORDER BY trade_date
                ROWS BETWEEN 249 PRECEDING AND CURRENT ROW
            ) AS hi250,
            MIN(close) OVER (
                PARTITION BY code ORDER BY trade_date
                ROWS BETWEEN 249 PRECEDING AND CURRENT ROW
            ) AS lo250
        FROM stock_daily
        WHERE trade_date BETWEEN :warm AND :end
          AND close IS NOT NULL
    )
    SELECT
        trade_date,
        SUM(CASE WHEN rn >= 20 AND close > ma20 THEN 1 ELSE 0 END)  AS above20,
        SUM(CASE WHEN rn >= 20 THEN 1 ELSE 0 END)                   AS denom20,
        SUM(CASE WHEN rn >= 60 AND close > ma60 THEN 1 ELSE 0 END)  AS above60,
        SUM(CASE WHEN rn >= 60 THEN 1 ELSE 0 END)                   AS denom60,
        SUM(CASE WHEN rn >= 250 AND close > ma250 THEN 1 ELSE 0 END) AS above250,
        SUM(CASE WHEN rn >= 250 THEN 1 ELSE 0 END)                   AS denom250,
        SUM(CASE WHEN rn >= 60 AND close >= hi60 THEN 1 ELSE 0 END)  AS high60,
        SUM(CASE WHEN rn >= 60 AND close <= lo60 THEN 1 ELSE 0 END)  AS low60,
        SUM(CASE WHEN rn >= 250 AND close >= hi250 THEN 1 ELSE 0 END) AS high250,
        SUM(CASE WHEN rn >= 250 AND close <= lo250 THEN 1 ELSE 0 END) AS low250
    FROM bars
    GROUP BY trade_date
    """
)


def _trade_calendar(session, start: date, end: date) -> list[date]:
    """区间内库里有情绪数据的交易日（升序）。"""
    return list(
        session.scalars(
            select(MarketSentiment.trade_date)
            .where(MarketSentiment.trade_date >= start, MarketSentiment.trade_date <= end)
            .order_by(MarketSentiment.trade_date)
        )
    )


def _pct(above: int | None, denom: int | None) -> float | None:
    """占比百分数。分母为 0 / None（历史不足）时返回 None，**不返回 0**。"""
    if not denom:
        return None
    return round((above or 0) / denom * 100, 2)


def compute_range(start: date, end: date) -> list[dict]:
    """算出 `[start, end]` 内每个交易日的宽度值，返回可直接写库的行。

    SQL 会多扫 `WARMUP_DAYS` 天喂窗口；只有落在 `[start, end]` 里的交易日产出。
    """
    warm = start - timedelta(days=WARMUP_DAYS)
    with session_scope() as session:
        targets = set(_trade_calendar(session, start, end))
        if not targets:
            return []
        aggregated = session.execute(
            _BREADTH_SQL, {"warm": warm, "end": end}
        ).mappings().all()

    rows: list[dict] = []
    for row in aggregated:
        day = row["trade_date"]
        if day not in targets:
            continue
        denom20 = row["denom20"] or 0
        rows.append(
            {
                "trade_date": day,
                "above_ma20": _pct(row["above20"], denom20),
                "above_ma60": _pct(row["above60"], row["denom60"]),
                "above_ma250": _pct(row["above250"], row["denom250"]),
                "high60": int(row["high60"] or 0) if denom20 else None,
                "low60": int(row["low60"] or 0) if denom20 else None,
                # 250 档分母为 0（历史没那么长）时，新高新低同样无意义 —— 留 NULL
                "high250": int(row["high250"] or 0) if row["denom250"] else None,
                "low250": int(row["low250"] or 0) if row["denom250"] else None,
                "universe_count": int(denom20) if denom20 else None,
            }
        )
    return rows


def collect_range(start: date, end: date) -> int:
    """算 `[start, end]` 并写库（幂等 upsert），返回写入行数。"""
    rows = compute_range(start, end)
    if not rows:
        return 0
    with session_scope() as session:
        upsert_many(session, BreadthDaily, rows)
    return len(rows)


def latest_trade_date() -> date | None:
    """库里有情绪数据的最近交易日（与恐贪同口径）。"""
    with session_scope() as session:
        return session.scalar(select(func.max(MarketSentiment.trade_date)))


def _has_rows() -> bool:
    with session_scope() as session:
        return bool(session.scalar(select(func.count()).select_from(BreadthDaily)))


def backfill(days: int = DEFAULT_BACKFILL_DAYS) -> int:
    """回看 `days` 个自然日做一次全量回补。"""
    end = latest_trade_date()
    if end is None:
        logger.warning("市场情绪表为空，无法回补市场宽度")
        return 0
    start = end - timedelta(days=days)
    written = collect_range(start, end)
    logger.info("市场宽度回补 %s ~ %s：写入 %d 天", start, end, written)
    return written


def refresh(days: int = REFRESH_DAYS) -> int:
    """重算最近 `days` 个交易日；空表时自动退回全量回补（新部署第一趟自带历史）。"""
    if not _has_rows():
        return backfill()

    end = latest_trade_date()
    if end is None:
        return 0
    with session_scope() as session:
        start = session.scalar(
            select(func.min(MarketSentiment.trade_date)).where(
                MarketSentiment.trade_date.in_(
                    select(MarketSentiment.trade_date)
                    .order_by(MarketSentiment.trade_date.desc())
                    .limit(days)
                    .scalar_subquery()
                )
            )
        )
    if start is None:
        start = end - timedelta(days=days * 2)
    return collect_range(start, end)
