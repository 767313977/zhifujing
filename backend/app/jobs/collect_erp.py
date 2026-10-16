"""ERP 股债性价比采集（沪深300 盈利收益率 − 10Y 国债收益率）。

- `pe_ttm`：沪深300 滚动市盈率，走 **iFinD EDB**（日频，列名
  `滚动市盈率(TTM):沪深300（单位：倍）`）。回补按 `EDB_CHUNK_DAYS` 天一段
  （380 天 ≈ 5 次调用），日常每天 1 次调用。这是本采集器**唯一的配额消耗**，
  失败按增强读数处理（尾部链登记 `erp`，不拖垮其它步骤）。
- `bond_yield`：中国 10 年期国债收益率，走 **akshare** `bond_zh_us_rate`
  （一次请求取全历史、本地过滤，**零配额**）。

`erp` = 100 / PE − 10Y（百分点）。**ERP 越高 = 股票相对债券越便宜**。
历史分位（滚动 250 交易日）在 API 读取时现算（复用恐贪的
`services/fear_greed.percentile_score`），这里只落原始值。

两个口径决定：
1. PE 或国债缺一项的日子**整天跳过**（不写半行 null —— 半行会让「分位窗口里
   有多少有效点」变得说不清）；国债周末也披露，但 PE 只覆盖交易日，交集天然
   收敛到交易日。
2. 日历骨架取 `market_sentiment.trade_date`，与恐贪、宽度、基差同口径。
"""

import logging
from datetime import date, timedelta

from sqlalchemy import func, select

from app.config import get_settings
from app.db import session_scope, upsert_many
from app.models import ErpDaily, MarketSentiment
from app.sources.ifind import IfindClient
from app.sources.markdown_table import to_float

logger = logging.getLogger(__name__)

# 回补默认的回看自然日数（≈ 一年出头），与基差 / 宽度的回补深度对齐
DEFAULT_BACKFILL_DAYS = 380

# 每次采集重算最近这么多个交易日（订正 / 漏一天下次自动补）
REFRESH_DAYS = 30

# EDB 单次区间的上限（自然日）。实测区间开太大返回行数会被截断，与
# `collect_funds.EDB_CHUNK_DAYS` 同一个经验值
EDB_CHUNK_DAYS = 90

# 目标指数（目前是固定一行口径，表结构留着以防以后加别的指数）
INDEX_CODE = "000300.SH"
INDEX_NAME = "沪深300"

# EDB 指标列名前缀（自然语言问句里也要用同花顺原始指标名，否则模糊匹配会跑偏，
# 见 `IfindClient.edb_data` 的说明）
PE_PREFIX = "滚动市盈率"


def _as_date(raw: object) -> date | None:
    """EDB / akshare 的日期是 str 或 Timestamp，统一归一成 date。"""
    try:
        return date.fromisoformat(str(raw).strip()[:10])
    except ValueError:
        return None


def _pe_cell(row: dict) -> float | None:
    """按列名前缀找 PE 那一列（列名带单位后缀，写死全名会在单位调整后静默取不到）。"""
    for key, raw in row.items():
        if str(key).startswith(PE_PREFIX):
            return to_float(raw)
    return None


def _fetch_pe_chunk(ifind: IfindClient, start: date, end: date) -> dict[date, float]:
    """取一段区间的 PE-TTM；整列缺失时重问一次（EDB 偶发丢列，见 collect_funds）。"""
    query = f"沪深300指数的市盈率TTM的日度数据，时间范围 {start.isoformat()} 至 {end.isoformat()}"
    _, rows = ifind.edb_data(query)
    values = {
        day: pe
        for row in rows
        if (day := _as_date(row.get("日期"))) is not None
        if (pe := _pe_cell(row)) is not None
    }
    if values:
        return values
    # 整列没回来（一行有效值都没有）才重问一次；有值但个别天空是正常披露节奏
    logger.warning("EDB 沪深300 PE %s ~ %s 整列缺失，重问一次", start, end)
    _, again = ifind.edb_data(query)
    return {
        day: pe
        for row in again
        if (day := _as_date(row.get("日期"))) is not None
        if (pe := _pe_cell(row)) is not None
    }


def _fetch_pe(ifind: IfindClient, start: date, end: date) -> dict[date, float]:
    """分段取 PE-TTM 并拼接。"""
    values: dict[date, float] = {}
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=EDB_CHUNK_DAYS - 1), end)
        values.update(_fetch_pe_chunk(ifind, cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return values


def _fetch_bond_yield() -> dict[date, float]:
    """10Y 国债收益率全历史（akshare，一次请求），本地过滤。"""
    import akshare as ak  # 延迟导入：scheduler 启动不背这个重依赖

    frame = ak.bond_zh_us_rate()
    values: dict[date, float] = {}
    for raw_day, raw_yield in zip(
        frame["日期"], frame["中国国债收益率10年"], strict=True
    ):
        day = _as_date(raw_day)
        number = to_float(raw_yield)
        if day is not None and number is not None:
            values[day] = number
    return values


def compute_range(ifind: IfindClient, start: date, end: date) -> list[dict]:
    """算 `[start, end]` 内每个交易日的 ERP，返回可直接写库的行。"""
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

    pe_map = _fetch_pe(ifind, start, end)
    bond_map = _fetch_bond_yield()

    rows: list[dict] = []
    for day in sorted(targets):
        pe = pe_map.get(day)
        bond = bond_map.get(day)
        # 缺一项整天跳过（PE 为 0 是数据源错误，同样跳过 —— 100/0 会爆出假 ERP）
        if pe is None or pe == 0 or bond is None:
            continue
        rows.append(
            {
                "trade_date": day,
                "index_code": INDEX_CODE,
                "index_name": INDEX_NAME,
                "pe_ttm": round(pe, 3),
                "bond_yield": round(bond, 4),
                "erp": round(100 / pe - bond, 4),
            }
        )
    return rows


def collect_range(ifind: IfindClient, start: date, end: date) -> int:
    """算 `[start, end]` 并写库（幂等 upsert），返回写入行数。"""
    rows = compute_range(ifind, start, end)
    if not rows:
        return 0
    with session_scope() as session:
        upsert_many(session, ErpDaily, rows)
    return len(rows)


def latest_trade_date() -> date | None:
    """库里有情绪数据的最近交易日（与恐贪、宽度同口径）。"""
    with session_scope() as session:
        return session.scalar(select(func.max(MarketSentiment.trade_date)))


def _has_rows() -> bool:
    with session_scope() as session:
        return bool(session.scalar(select(func.count()).select_from(ErpDaily)))


def backfill(days: int = DEFAULT_BACKFILL_DAYS) -> int:
    """回看 `days` 个自然日做一次全量回补（PE 分段取，约 5 次 iFinD 调用）。"""
    end = latest_trade_date()
    if end is None:
        logger.warning("市场情绪表为空，无法回补 ERP")
        return 0
    start = end - timedelta(days=days)
    written = collect_range(IfindClient(get_settings()), start, end)
    logger.info("ERP 回补 %s ~ %s：写入 %d 天", start, end, written)
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
    return collect_range(IfindClient(get_settings()), start, end)
