"""股指期货持仓排名采集（中金所，**零 iFinD 配额**）。

挂在 17:30 那一趟（`collect_daily.run_late`）采当天：中金所约收盘后 16:00 才
发布，15:05 的主采集拿不到 —— 与龙虎榜「当天晚些才有」是同一类问题。

数据是四个品种（IF / IC / IH / IM）各合约的前 20 席位持仓排名，这里只留两个
口径：「前 20 合计」与「中信期货」。口径细节见 `models.FuturesPosition`。

## 为什么不用 iFinD

中金所官网直接给前 20 名明细，akshare 已经解析好；iFinD 的期货持仓接口要另外
计费、字段还更绕。这一项全程零配额，配额再紧也照采。
"""

import logging
from datetime import date

from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import session_scope, upsert_many
from app.models import FuturesPosition, TradeCalendar
from app.sources.akshare_source import _call_bounded
from app.timeutil import today_cn

logger = logging.getLogger(__name__)

#: 四个股指期货品种 → 简报里用的中文名（顺序即展示顺序）
VARIETIES = {"IF": "沪深300", "IC": "中证500", "IH": "上证50", "IM": "中证1000"}
#: 中信期货席位名（中金所原样返回，带「(代客)」后缀）
CITIC = "中信期货(代客)"
#: 合计行的 rank。中金所自己给合计，**不要**再把 rank 1~20 求和（会正好翻倍）
SUMMARY_RANK = 999


def _is_trade_day(day: date) -> bool:
    with session_scope() as session:
        return (
            session.scalar(
                select(TradeCalendar.trade_date).where(TradeCalendar.trade_date == day)
            )
            is not None
        )


def parse(day: date, tables: dict) -> list[dict]:
    """中金所返回值 → 每个品种一行。纯函数，便于单测。

    `tables` 是 `{合约代码: DataFrame}`（akshare 的返回结构），按品种把当天四个
    合约（当月 / 下月 / 当季 / 下季）加总。当月合约在交割日当天持仓为 0，属正常。
    """
    totals: dict[str, list[int]] = {}
    for symbol, frame in tables.items():
        variety = str(symbol)[:2]
        if variety not in VARIETIES or frame is None or frame.empty:
            continue
        summary = frame[frame["rank"] == SUMMARY_RANK]
        if summary.empty:
            continue
        row = summary.iloc[0]
        acc = totals.setdefault(variety, [0, 0, 0, 0])
        acc[0] += int(
            round(
                float(row["short_open_interest"]) - float(row["long_open_interest"])
            )
        )
        acc[1] += int(
            round(
                float(row["short_open_interest_chg"])
                - float(row["long_open_interest_chg"])
            )
        )
        # 中信：多空两个榜分开取（同一行的多头榜与空头榜不是同一家）
        acc[2] += int(
            round(
                float(
                    frame.loc[
                        frame["short_party_name"] == CITIC, "short_open_interest"
                    ].sum()
                )
                - float(
                    frame.loc[
                        frame["long_party_name"] == CITIC, "long_open_interest"
                    ].sum()
                )
            )
        )
        acc[3] += int(
            round(
                float(
                    frame.loc[
                        frame["short_party_name"] == CITIC, "short_open_interest_chg"
                    ].sum()
                )
                - float(
                    frame.loc[
                        frame["long_party_name"] == CITIC, "long_open_interest_chg"
                    ].sum()
                )
            )
        )

    return [
        {
            "trade_date": day,
            "variety": variety,
            "top_net_short": acc[0],
            "top_change": acc[1],
            "citic_net_short": acc[2],
            "citic_change": acc[3],
        }
        for variety, acc in sorted(totals.items())
    ]


def collect(day: date | None = None, settings: Settings | None = None) -> int:
    """采一个交易日的期指持仓，返回写入的品种数。"""
    settings = settings or get_settings()
    target = day or today_cn()
    if not _is_trade_day(target):
        logger.info("%s 不是交易日，跳过期指持仓", target)
        return 0

    import akshare as ak  # 延迟导入：调度启动不背这个重依赖

    raw = _call_bounded(
        lambda: ak.get_cffex_rank_table(
            date=target.strftime("%Y%m%d"), vars_list=list(VARIETIES)
        ),
        f"{target} 期指持仓",
        # 4 个品种 × 4 个合约，实测十几秒；给足余量但不无限等（akshare 无超时）
        max(float(settings.http_timeout), 90.0),
    )
    rows = parse(target, raw)
    if not rows:
        # 多半是 16:00 前还没发布。记 failed，19:30 的兜底会再试一次
        raise ValueError(f"{target} 没取到期指持仓（中金所可能还没发布）")

    with session_scope() as session:
        written = upsert_many(session, FuturesPosition, rows)
    logger.info("期指持仓采集完成：%s，%d 个品种", target, written)
    return written
