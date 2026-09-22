"""个股 DDE / 主力净流入的**按需抓取**（iFinD 口径）。

不是定时任务：某只票的个股页打开、而库里没有最近交易日的数据时才拉一次，
之后读库 —— 与板块成分股（`collect_sectors.SectorCollector.collect_members`）
同一套路数。表的口径与两条坑写在 `models.StockDde` 的 docstring 里。

为什么只能靠 iFinD：
- akshare 的 `stock_fund_flow_*` 是**同花顺公开页**的资金流（个股 / 概念 / 行业 /
  大单追踪），只有「即时 / 3日 / 5日 / 10日」四个窗口、**不给历史**，口径也不是 DDE；
- 东财那个集群本站不碰（实测会触发本机 IP 频控，见设计文档 1.7）。

所以每次现取都花 1 次 iFinD 配额 —— 别在这个入口上做全市场循环。
"""

import logging
from datetime import date, datetime, time

from sqlalchemy import select

from app.db import session_scope, upsert_fill
from app.models import StockDde, TradeCalendar
from app.sources.ifind import IfindClient, IfindError, normalize_code
from app.sources.markdown_table import pick_float, pick_text

logger = logging.getLogger(__name__)

# 一次取多少个交易日：60 ≈ 一个季度，够看趋势；要更长由调用方传 days
DEFAULT_DAYS = 60

# 与 `collect_flows.CLOSE_READY` 同一道守卫、同一个理由：**早于这个时刻取「当天」，
# 拿到的是盘中瞬时快照，不是收盘终值** —— 标成当天的数就是编数据（那是给复盘用的，
# 不要求实时）。所以盘中抓回来的「今天」那一行直接丢掉不落库。
CLOSE_READY = time(15, 5)

# 来源单次回答的**行数上限**（与配额无关，是接口自己的输出上限）：实测请求「近 120 个
# 交易日」和「近 250 个交易日」都只回 **100 行**，并在回答里另起一句提示。两种提示文案
# 都认（新版「以下为部分数据」、旧版「数据过大」）。命中就说明这次只拿到了最近一段。
_TRUNCATED_HINTS = ("以下为部分数据", "数据过大")


def _query(code: str, days: int) -> str:
    """自然语言取数语句。

    ⚠️ 指标名要写**来源自己的叫法**（`5日DDE`）。实测写成「DDX」时它不会报错，
    而是悄悄退回「主力资金流向」那一列 —— 表看起来正常、但没有 DDE，属于最难
    发现的那类错，所以这里写死来源的指标名。
    """
    return f"{code} 近{days}个交易日 的 主力净流入额 与 5日DDE"


def _row_date(text: str | None) -> date | None:
    """iFinD 的日期列是 `20260922` 这种 8 位数字。"""
    if not text or len(text) != 8 or not text.isdigit():
        return None
    return date(int(text[:4]), int(text[4:6]), int(text[6:]))


def collect_stock_dde(code: str, days: int = DEFAULT_DAYS) -> tuple[int, bool]:
    """抓一次并落库。返回（写入行数, 是否被来源截断）。调用方负责兜 `IfindError`。"""
    symbol = normalize_code(code)
    answer, rows = IfindClient().stock_performance(_query(symbol, days))

    # 被截断时必须说出去：只拿到最近 100 行，画出来的窗口比用户要的短
    truncated = any(hint in answer for hint in _TRUNCATED_HINTS)
    if truncated:
        logger.warning(
            "个股 %s 的 DDE 被来源截断：请求 %d 个交易日，只回了 %d 行",
            symbol,
            days,
            len(rows),
        )

    parsed: list[dict] = []
    for row in rows:
        trade_date = _row_date(pick_text(row, "日期"))
        if trade_date is None:
            continue
        parsed.append(
            {
                "trade_date": trade_date,
                "code": symbol,
                # 空单元格（周末那两行）会解析成 None，原样存 —— 不填 0，
                # 「没有数据」和「净流入为零」是两件事
                "net_inflow": pick_float(row, "主力净流入"),
                "dde": pick_float(row, "DDE"),
            }
        )
    if not parsed:
        logger.warning("iFinD 没返回 %s 的 DDE 行", symbol)
        return 0, truncated

    # 两轮过滤，理由不同：
    # ① **非交易日**：来源会把周末也列出来（净流入为空、DDE 延续前一交易日的值），
    #    不过滤就会落进「周六也有资金流入」这种假数据；
    # ② **今天还没收盘**：盘中拿到的是瞬时快照，见 `CLOSE_READY`。
    start = min(item["trade_date"] for item in parsed)
    end = max(item["trade_date"] for item in parsed)
    today = date.today()
    before_close = datetime.now().time() < CLOSE_READY
    with session_scope() as session:
        trade_days = set(
            session.scalars(
                select(TradeCalendar.trade_date).where(
                    TradeCalendar.trade_date >= start,
                    TradeCalendar.trade_date <= end,
                )
            )
        )
        fresh = [item for item in parsed if item["trade_date"] in trade_days]
        settled = [
            item
            for item in fresh
            if not (item["trade_date"] == today and before_close)
        ]
        # upsert_fill 而不是 upsert：来源偶发给空值，而空值不该把已经采到的数抹掉
        written = upsert_fill(session, StockDde, settled)

    logger.info(
        "个股 %s 的 DDE：取回 %d 行 → 去掉 %d 行非交易日、%s，写入 %d 行",
        symbol,
        len(parsed),
        len(parsed) - len(fresh),
        "今天还没收盘（丢掉盘中快照）" if before_close else "无需丢弃今日行",
        written,
    )
    return written, truncated
