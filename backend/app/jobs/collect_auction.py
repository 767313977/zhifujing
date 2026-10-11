"""集合竞价采样：交易日 9:15–9:26 每分钟抓一次全市场快照 → `auction_snapshot`。

数据源是**腾讯批量行情**（`sources/tencent.fetch_spot`），零 iFinD 配额。

为什么要逐分钟：集合竞价的**过程**本身就是信息（9:20 撤单截止前后的量跳变、
最后 1 分钟的急涌），只留 9:25 的终值就只剩一个数了。每跑一次就往后追加一帧
`[HHMM, 价, 累计成交额]`，同时把「9:25 竞价结果」那几列刷成当前帧 ——
窗口在 9:26 截止，所以最后一帧正好是竞价终值、又没掺进开盘后的连续竞价。

⚠️ 与收盘采集**完全解耦**：
- **不进 `collect_guard`** —— 那把锁是给「收盘后那几趟串行跑」用的，这里是每分钟一跑；
- **不写 `collect_log`** —— 它不是「每日一采」（一天 12 次），写进去就是 12 条重复记录；
- **无启动补采** —— 竞价数据没有历史可补（腾讯只给当日实时），错过就错过。
"""

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

from app.db import session_scope, upsert_many
from app.models import AuctionSnapshot, StockBasic, TradeCalendar
from app.sources.tencent import fetch_spot

logger = logging.getLogger(__name__)

# 采样窗口（HHMM）。9:15 集合竞价开始、9:25 出开盘价，9:26 取最后一帧后收手 ——
# 停在 9:26（而不是 9:30）是为了拿「竞价终值」又不把开盘后的连续竞价掺进来。
FIRST_SAMPLE = "0915"
LAST_SAMPLE = "0926"

_CST = timezone(timedelta(hours=8))


def now_cst() -> datetime:
    """当前本地时间（Asia/Shanghai）。容器时区不可信，显式按固定偏移取。"""
    return datetime.now(_CST)


def _is_trade_day(day: date) -> bool:
    """查交易日历。日历为空时保守返回 False（全新部署的库会先跑一遍采集建日历）。"""
    with session_scope() as session:
        return bool(
            session.scalar(
                select(func.count())
                .select_from(TradeCalendar)
                .where(TradeCalendar.trade_date == day)
            )
        )


def _universe() -> list[str]:
    """全市场代码（`stock_basic` 覆盖全 A，约 5500 只）。"""
    with session_scope() as session:
        return [code for code in session.scalars(select(StockBasic.code)) if code]


def collect_auction(now: datetime | None = None) -> int:
    """跑一次采样，返回写入的股票数（0 表示跳过 / 无数据）。

    `now` 可注入（测试用）；缺省取当前本地时间。
    """
    moment = now or now_cst()
    day = moment.date()
    hhmm = moment.strftime("%H%M")
    if not (FIRST_SAMPLE <= hhmm <= LAST_SAMPLE):
        return 0
    if not _is_trade_day(day):
        return 0

    codes = _universe()
    if not codes:
        logger.warning("竞价采样：stock_basic 为空，跳过（先跑一轮采集建池）")
        return 0

    try:
        quotes = fetch_spot(codes)
    except Exception:  # noqa: BLE001 - 单分钟的失败不该影响下一分钟
        logger.warning("竞价采样：抓取失败 %s %s", day, hhmm, exc_info=True)
        return 0
    if not quotes:
        logger.warning("竞价采样：%s %s 返回 0 行", day, hhmm)
        return 0

    with session_scope() as session:
        # 已有的逐分钟序列：读回来往后追加（`upsert_many` 是整列覆盖，不读会丢掉前面几帧）
        existing = {
            code: samples
            for code, samples in session.execute(
                select(AuctionSnapshot.code, AuctionSnapshot.samples).where(
                    AuctionSnapshot.trade_date == day
                )
            ).all()
        }
        rows: list[dict] = []
        for item in quotes:
            code = item["code"]
            price = item["price"]
            pre_close = item["pre_close"]
            samples = list(existing.get(code) or [])
            samples.append([hhmm, price, item["amount"]])
            rows.append(
                {
                    "trade_date": day,
                    "code": code,
                    "name": item["name"],
                    "price": price,
                    "pre_close": pre_close,
                    # 竞价涨幅：竞价价 / 昨收 − 1。昨收缺失时留 None，不伪造成 0
                    "pct_chg": (
                        round((price / pre_close - 1) * 100, 2)
                        if price is not None and pre_close
                        else None
                    ),
                    "open": item["open"],
                    "amount": item["amount"],
                    "volume": item["volume"],
                    "turnover": item["turnover"],
                    "volume_ratio": item["volume_ratio"],
                    "float_mv": item["float_mv"],
                    "total_mv": item["total_mv"],
                    "limit_up": item["limit_up"],
                    "limit_down": item["limit_down"],
                    "samples": samples,
                }
            )
        upsert_many(session, AuctionSnapshot, rows)

    logger.info("竞价采样 %s %s：%d 只", day, hhmm, len(rows))
    return len(rows)
