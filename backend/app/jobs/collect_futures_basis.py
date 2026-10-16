"""期指年化基差采集（akshare，**零 iFinD 配额**）。

四个品种：IF（沪深300）/ IH（上证50）/ IC（中证500）/ IM（中证1000）。

- 期货价：`ak.futures_main_sina` 主力连续日收盘（IF0 / IH0 / IC0 / IM0）
- 现货价：`ak.stock_zh_index_daily`（sh000300 / sh000016 / sh000905 / sh000852）

口径（前端口径说明里同样写明）：

1. `basis_pct` = (期货 − 现货) / 现货 × 100（%）。**负值 = 贴水** ——
   贴水越深，中性策略的空头对冲成本越高，是资金对后市谨慎的信号。
2. `annual_pct` = basis_pct × 365 / 距交割日天数。交割日 = 交割月第三个周五；
   主力连续**近似当月合约**：距交割不足 `ROLLOVER_MIN_DAYS` 天就按下月合约算，
   避免年化除以个位数天数爆出无意义的尖刺。**这个天数必须对上 akshare 主力
   按成交量提前换月的节奏（交割前约 7 天）**，否则错位期会拿**当月**交割日做
   分母、把年化放大（原委见 `ROLLOVER_MIN_DAYS`）。原始基差不受换月口径影响。
3. 期货与现货**都有**收盘价的交易日才出值；缺一整天跳过（不写 0 ——
   0% 是「期现完全平水」，是个真实结论）。

日历骨架取 `market_sentiment.trade_date`，与恐贪、市场宽度同口径。
自愈策略也一样：`refresh()` 重算最近 `REFRESH_DAYS` 个交易日，空表自动退回
全量回补（新部署第一趟自带历史）。
"""

import logging
from datetime import date, timedelta

from sqlalchemy import func, select

from app.db import session_scope, upsert_many
from app.models import FuturesBasisDaily, MarketSentiment

logger = logging.getLogger(__name__)

# 回补默认的回看自然日数（≈ 一年出头），与宽度 / 恐贪的回补深度对齐
DEFAULT_BACKFILL_DAYS = 380

# 每次采集重算最近这么多个交易日（订正 / 漏一天下次自动补，与宽度同策略）
REFRESH_DAYS = 30

# 距交割不足这么多天就按下月合约年化。
#
# 标定成 **8**（2026-10-16 从 4 改上来）：akshare 的主力连续是**按成交量换月**的，
# 实测通常交割前约 7 天就切到了下月合约。日历逻辑若只提前 4 天，那 4~7 天里
# akshare 给的已是下月价、我们却仍拿**当月**交割日当年化分母（分母被缩到 6~7 天
# 而非约 36 天），年化于是被放大成假尖刺。提到 8 让日历换月跟上实际换月。
# 代价是交割前 4~7 天那段的基差按「下月合约」口径 —— 但这**正是** akshare 实际
# 在给的口径，所以反而更自洽（原始 `basis_pct` 与换月口径无关，不受影响）。
ROLLOVER_MIN_DAYS = 8

# 品种配置：期货主力连续代码 / 现货指数代码 / 现货名称
CONTRACTS: dict[str, dict[str, str]] = {
    "IF": {"future": "IF0", "spot": "sh000300", "name": "沪深300"},
    "IH": {"future": "IH0", "spot": "sh000016", "name": "上证50"},
    "IC": {"future": "IC0", "spot": "sh000905", "name": "中证500"},
    "IM": {"future": "IM0", "spot": "sh000852", "name": "中证1000"},
}


def _as_date(raw: object) -> date | None:
    """akshare 的日期列是 str 或 pandas Timestamp，统一归一成 date。"""
    try:
        return date.fromisoformat(str(raw).strip()[:10])
    except ValueError:
        return None


def _third_friday(year: int, month: int) -> date:
    """交割月第三个周五（股指期货交割日）。"""
    first = date(year, month, 1)
    # weekday()：周一 0 … 周五 4
    offset = (4 - first.weekday()) % 7
    return first + timedelta(days=offset + 14)


def _days_to_expiry(day: date) -> int:
    """`day` 距「当前主力合约」交割日的自然日数（近似）。

    距本月交割不足 `ROLLOVER_MIN_DAYS` 天就滚到下月 —— 这个天数要跟上 akshare
    主力的**成交量换月**（交割前约 7 天），否则错位期会拿当月交割日做分母、
    把年化放大（原委见 `ROLLOVER_MIN_DAYS`）。
    """
    expiry = _third_friday(day.year, day.month)
    days = (expiry - day).days
    if days < ROLLOVER_MIN_DAYS:
        year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
        days = (_third_friday(year, month) - day).days
    return days


def _fetch_contract(code: str, cfg: dict[str, str]) -> list[dict]:
    """取一个品种的期现收盘序列，返回可直接写库的行（未按日历过滤）。"""
    import akshare as ak  # 延迟导入：scheduler 启动不背这个重依赖

    futures = ak.futures_main_sina(symbol=cfg["future"])
    spot = ak.stock_zh_index_daily(symbol=cfg["spot"])

    future_close = {}
    for raw_day, raw_close in zip(futures["日期"], futures["收盘价"], strict=True):
        day = _as_date(raw_day)
        if day is not None and raw_close is not None:
            future_close[day] = float(raw_close)
    spot_close = {}
    for raw_day, raw_close in zip(spot["date"], spot["close"], strict=True):
        day = _as_date(raw_day)
        if day is not None and raw_close is not None:
            spot_close[day] = float(raw_close)

    rows: list[dict] = []
    for day, f_close in future_close.items():
        s_close = spot_close.get(day)
        # 期现不同步的日子（期货有夜盘暗示、指数临时缺数）整天跳过，不硬凑
        if s_close is None or s_close == 0:
            continue
        days = _days_to_expiry(day)
        basis = (f_close - s_close) / s_close * 100
        rows.append(
            {
                "trade_date": day,
                "code": code,
                "name": cfg["name"],
                "future_close": round(f_close, 2),
                "spot_close": round(s_close, 3),
                "basis_pct": round(basis, 4),
                "annual_pct": round(basis * 365 / days, 2),
                "days_to_expiry": days,
            }
        )
    return rows


def compute_range(start: date, end: date) -> list[dict]:
    """算 `[start, end]` 内每个交易日的四品种基差，返回可直接写库的行。

    四个品种各自拉全历史（一次请求一个），再在本地按日历骨架过滤 ——
    8 次 akshare 请求换来全部历史，比按段请求简单且零配额。
    """
    with session_scope() as session:
        targets = set(
            session.scalars(
                select(MarketSentiment.trade_date).where(
                    MarketSentiment.trade_date >= start,
                    MarketSentiment.trade_date <= end,
                )
            )
        )
    if not targets:
        return []

    rows: list[dict] = []
    for code, cfg in CONTRACTS.items():
        try:
            contract_rows = _fetch_contract(code, cfg)
        except Exception:  # noqa: BLE001 - 单品种失败不该拖垮其它三个
            logger.exception("期指基差 %s 拉取失败", code)
            continue
        rows.extend(row for row in contract_rows if row["trade_date"] in targets)
    return rows


def collect_range(start: date, end: date) -> int:
    """算 `[start, end]` 并写库（幂等 upsert），返回写入行数。"""
    rows = compute_range(start, end)
    if not rows:
        return 0
    with session_scope() as session:
        upsert_many(session, FuturesBasisDaily, rows)
    return len(rows)


def latest_trade_date() -> date | None:
    """库里有情绪数据的最近交易日（与恐贪、宽度同口径）。"""
    with session_scope() as session:
        return session.scalar(select(func.max(MarketSentiment.trade_date)))


def _has_rows() -> bool:
    with session_scope() as session:
        return bool(session.scalar(select(func.count()).select_from(FuturesBasisDaily)))


def backfill(days: int = DEFAULT_BACKFILL_DAYS) -> int:
    """回看 `days` 个自然日做一次全量回补。"""
    end = latest_trade_date()
    if end is None:
        logger.warning("市场情绪表为空，无法回补期指基差")
        return 0
    start = end - timedelta(days=days)
    written = collect_range(start, end)
    logger.info("期指基差回补 %s ~ %s：写入 %d 行", start, end, written)
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
