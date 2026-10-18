"""个股最新一期财务摘要：按需拉取 + 7 天缓存（杀猪盘体检的信号 6）。

**为什么不做全市场批量**：只有一个页面（个股体检）用得到，5000 只票一次拉一遍
既慢又没必要；而同花顺这条接口是单只查询。改成「打开哪只票就取哪只」之后，
每天实际只会有几次请求，且 7 天内重复打开都走缓存（财报是季度数据，7 天足够保守）。

**取不到时怎么办**：有旧缓存就用旧的（由调用方判断报告期是否过期），没有就返回
`None` —— 上层把信号记成 `unknown`，不拿 0 分冒充「基本面健康」。
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime

from app.db import session_scope, upsert
from app.models import StockFinancial
from app.sources.akshare_source import AkshareSource
from app.sources.markdown_table import to_float

logger = logging.getLogger(__name__)

# 缓存天数：财报按季度更新，7 天足够；也让「同一只票反复打开」只发一次请求。
CACHE_DAYS = 7

# 报告期超过这么多天就算「过期」—— 正常公司一年内该有 4 次报告，200 天还没更新
# 多半是停牌/退市/数据源断了，此时不该拿它下结论。
REPORT_MAX_AGE_DAYS = 200


@dataclass(slots=True)
class Snapshot:
    code: str
    report_period: date
    revenue: float | None
    revenue_yoy: float | None
    net_profit: float | None
    net_profit_yoy: float | None
    deducted_net_profit: float | None
    deducted_net_profit_yoy: float | None
    eps: float | None
    ocf_per_share: float | None
    roe: float | None
    gross_margin: float | None
    debt_ratio: float | None
    fetched_at: datetime | None


def _as_date(raw: object) -> date | None:
    try:
        return date.fromisoformat(str(raw or "").strip()[:10])
    except ValueError:
        return None


def _parse(records: list[dict]) -> dict | None:
    """从「按报告期」的宽表里取**最新一期**，并统一换算金额与百分比。"""
    rows: list[tuple[date, dict]] = []
    for record in records:
        period = _as_date(record.get("报告期"))
        if period is not None:
            rows.append((period, record))
    if not rows:
        return None
    report_period, latest = max(rows, key=lambda item: item[0])

    net_profit = to_float(latest.get("净利润"))
    revenue = to_float(latest.get("营业总收入"))
    if net_profit is None and revenue is None:
        return None
    return {
        "report_period": report_period,
        "revenue": revenue,
        "revenue_yoy": to_float(latest.get("营业总收入同比增长率")),
        "net_profit": net_profit,
        "net_profit_yoy": to_float(latest.get("净利润同比增长率")),
        # 扣非与每股指标：同一次请求里就有，用来剔除一次性损益、判断利润含金量
        "deducted_net_profit": to_float(latest.get("扣非净利润")),
        "deducted_net_profit_yoy": to_float(latest.get("扣非净利润同比增长率")),
        "eps": to_float(latest.get("基本每股收益")),
        "ocf_per_share": to_float(latest.get("每股经营现金流")),
        "roe": to_float(latest.get("净资产收益率")),
        "gross_margin": to_float(latest.get("销售毛利率")),
        "debt_ratio": to_float(latest.get("资产负债率")),
    }


def _snapshot(row: StockFinancial) -> Snapshot:
    return Snapshot(
        code=row.code,
        report_period=row.report_period,
        revenue=row.revenue,
        revenue_yoy=row.revenue_yoy,
        net_profit=row.net_profit,
        net_profit_yoy=row.net_profit_yoy,
        deducted_net_profit=row.deducted_net_profit,
        deducted_net_profit_yoy=row.deducted_net_profit_yoy,
        eps=row.eps,
        ocf_per_share=row.ocf_per_share,
        roe=row.roe,
        gross_margin=row.gross_margin,
        debt_ratio=row.debt_ratio,
        fetched_at=row.fetched_at,
    )


def latest(code: str) -> Snapshot | None:
    """取最新一期财务摘要；缓存 7 天，取不到时回落到旧缓存。"""
    cached: Snapshot | None = None
    with session_scope() as session:
        row = session.get(StockFinancial, code)
        if row is not None:
            cached = _snapshot(row)
            if row.fetched_at is not None and (
                datetime.now() - row.fetched_at
            ).days < CACHE_DAYS:
                return cached

    try:
        records = AkshareSource().financial_abstract_ths(code)
    except Exception:  # noqa: BLE001 - 取不到就回落，不能拖垮体检
        logger.exception("财务摘要取数失败：%s", code)
        return cached

    parsed = _parse(records)
    if parsed is None:
        logger.warning("%s 的财务摘要没有可用行", code)
        return cached

    row = {"code": code, **parsed, "fetched_at": datetime.now()}
    with session_scope() as session:
        upsert(session, StockFinancial, [row])
    return Snapshot(code=code, fetched_at=row["fetched_at"], **parsed)
