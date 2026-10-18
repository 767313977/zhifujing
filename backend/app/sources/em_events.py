"""个股公告 / 研报 / 解禁：东财数据中心三条免费源。

传输全部借 akshare（`stock_individual_notice_report` / `stock_research_report_em` /
`stock_restricted_release_queue_em`），这里只管**口径**：列名归一、数值转 float、
按日期排序并截断。三个上游都不在 `push2*.eastmoney.com` 集群，也不占 iFinD 配额。

**取不到就抛**（与 `em_news` 同一约定）：空列表代表「上游真的没有」，异常代表
「源挂了」。调用方（`services.stock_events`）据此决定是显示空态还是缓存/降级文案。
"""

import logging
import math
from dataclasses import dataclass
from datetime import date, timedelta

logger = logging.getLogger(__name__)

#: 公告回看天数。覆盖「最近几个月有没有立案 / 问询 / 减持计划」这类硬证据。
NOTICE_LOOKBACK_DAYS = 180
#: 一次最多留多少条公告（页面上只显示 10 条，留一些给杀猪盘体检扫关键词）
NOTICE_LIMIT = 30
#: 研报留多少条；解禁留多少条
REPORT_LIMIT = 10
UNLOCK_LIMIT = 10


@dataclass(frozen=True)
class NoticeItem:
    """一条公告。字段名与前端一一对应。"""

    published_at: str  # "2026-08-15"（东财原样给）
    title: str
    #: 东财的公告类型，常见「其他 / 半年度报告摘要 / 重大事项 / 风险提示」
    category: str
    url: str


@dataclass(frozen=True)
class ReportItem:
    """一条机构研报。"""

    published_at: str
    title: str
    institution: str
    #: 东财评级（买入 / 增持 / 中性 …）
    rating: str
    industry: str
    #: 盈利预测一行文本（如「2026E EPS 69.83 / PE 18.59」），没有预测时为空串
    forecast: str
    url: str


@dataclass(frozen=True)
class UnlockItem:
    """一笔限售解禁。

    ⚠️ 两个 `ratio_*` 是**比例**（0.05 = 5%），不是百分数 —— 上游给的就是小数，
    页面与阈值判断都要自己乘 100 / 比 0.05。
    """

    unlock_date: str
    #: 实际解禁数量（股）
    shares: float | None
    #: 实际解禁数量市值（元）
    market_value: float | None
    #: 占总市值比例（小数）
    ratio_total: float | None
    #: 占流通市值比例（小数）
    ratio_float: float | None
    share_type: str


def _text(value: object) -> str:
    """上游的 None / NaN 都归一成空串。"""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _num(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _day(value: object) -> str:
    """日期统一成 `YYYY-MM-DD`（上游偶尔给 `2026-08-15 00:00:00`）。"""
    return _text(value)[:10]


def _code(code: str) -> str:
    return str(code).strip().zfill(6)


def fetch_notices(
    code: str,
    *,
    days: int = NOTICE_LOOKBACK_DAYS,
    limit: int = NOTICE_LIMIT,
    today: date | None = None,
) -> list[NoticeItem]:
    """最近 `days` 天的公告，按日期倒序。

    `stock_individual_notice_report` 的 `begin_date` / `end_date` 是 `YYYYMMDD`；
    不传会退化成上游的默认窗口（不可控），所以这里总是显式给。
    """
    import akshare as ak

    end = today or date.today()
    begin = end - timedelta(days=max(1, days))
    df = ak.stock_individual_notice_report(
        security=_code(code),
        begin_date=begin.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
    )
    if df is None or getattr(df, "empty", True):
        return []

    rows = [
        NoticeItem(
            published_at=_day(rec.get("公告日期")),
            title=_text(rec.get("公告标题")),
            category=_text(rec.get("公告类型")),
            url=_text(rec.get("网址")),
        )
        for rec in df.to_dict("records")
    ]
    rows = [row for row in rows if row.title]
    rows.sort(key=lambda row: row.published_at, reverse=True)
    return rows[:limit]


def fetch_reports(code: str, *, limit: int = REPORT_LIMIT) -> list[ReportItem]:
    """机构研报，按报告日期倒序。

    上游没有「目标价」字段，只有未来三年的盈利预测 —— 有就拼成一行 `forecast`，
    没有就留空，**不拿评级去顶替目标价**。
    """
    import akshare as ak

    df = ak.stock_research_report_em(symbol=_code(code))
    if df is None or getattr(df, "empty", True):
        return []

    records = df.to_dict("records")
    columns = list(df.columns)
    # 年份列名随年份变（2026-盈利预测-收益），所以按后缀找第一组
    eps_col = next((name for name in columns if str(name).endswith("-盈利预测-收益")), None)
    pe_col = next((name for name in columns if str(name).endswith("-盈利预测-市盈率")), None)

    rows: list[ReportItem] = []
    for rec in records:
        title = _text(rec.get("报告名称"))
        if not title:
            continue
        forecast = ""
        if eps_col is not None:
            year = str(eps_col).split("-")[0]
            eps = _num(rec.get(eps_col))
            pe = _num(rec.get(pe_col)) if pe_col is not None else None
            if eps is not None:
                forecast = f"{year}E EPS {eps:.2f}"
                if pe is not None:
                    forecast += f" / PE {pe:.1f}"
        rows.append(
            ReportItem(
                published_at=_day(rec.get("日期")),
                title=title,
                institution=_text(rec.get("机构")),
                rating=_text(rec.get("东财评级")),
                industry=_text(rec.get("行业")),
                forecast=forecast,
                url=_text(rec.get("报告PDF链接")),
            )
        )
    rows.sort(key=lambda row: row.published_at, reverse=True)
    return rows[:limit]


def fetch_unlocks(
    code: str, *, limit: int = UNLOCK_LIMIT, today: date | None = None
) -> list[UnlockItem]:
    """未来的限售解禁，按解禁日**升序**（最近的排最前）。

    上游只给「还没解禁」的排期（已解禁的历史不会出现），所以这里只再兜一道
    「日期不早于今天」，防止缓存跨天后把昨天那笔还显示成「未来」。
    """
    import akshare as ak

    df = ak.stock_restricted_release_queue_em(symbol=_code(code))
    if df is None or getattr(df, "empty", True):
        return []

    floor = (today or date.today()).isoformat()
    rows = [
        UnlockItem(
            unlock_date=_day(rec.get("解禁时间")),
            shares=_num(rec.get("实际解禁数量")) or _num(rec.get("解禁数量")),
            market_value=_num(rec.get("实际解禁数量市值")),
            ratio_total=_num(rec.get("占总市值比例")),
            ratio_float=_num(rec.get("占流通市值比例")),
            share_type=_text(rec.get("限售股类型")),
        )
        for rec in df.to_dict("records")
    ]
    rows = [row for row in rows if row.unlock_date and row.unlock_date >= floor]
    rows.sort(key=lambda row: row.unlock_date)
    return rows[:limit]
