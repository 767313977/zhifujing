"""集合竞价采样：交易日 9:15–9:26 每分钟抓一次全市场快照 → `auction_snapshot`。

数据源是**腾讯批量行情**（`sources/tencent.fetch_spot`），零 iFinD 配额。

两个采样窗口，作用不同：
- **9:15–9:26**（`RESULT_*`）：竞价**结果** —— 竞价价 / 昨收 / 竞价涨幅 / 竞价额 /
  竞价量 / 换手 / 市值 / 涨跌停价，同时逐分钟追加 `samples`（竞价走势）。停在 9:26
  是为了拿竞价终值、又不把开盘后的连续竞价掺进来。
- **9:31**（`RATIO_SAMPLE`）：**只补 `volume_ratio`（量比）**。腾讯**开盘前不填量比**
  （实测 9:26 前恒为 0，开盘后才有值），所以单独再采一帧；这一帧**不改**竞价价格/金额，
  也不往 `samples` 里追加（那时的成交额已经含开盘后的连续交易，会污染走势）。

为什么要逐分钟存过程：9:20 撤单截止前后的量跳变、最后 1 分钟的急涌，只留终值就看不到了。

⚠️ 与收盘采集**完全解耦**：
- **不进 `collect_guard`** —— 那把锁是给「收盘后那几趟串行跑」用的，这里是每分钟一跑；
- **不写 `collect_log`** —— 它不是「每日一采」（一天十几轮），写进去就是重复记录；
- **无启动补采** —— 竞价数据没有历史可补（腾讯只给当日实时），错过就错过。
"""

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

from app.db import session_scope, upsert_many
from app.models import AuctionSnapshot, StockBasic, TradeCalendar
from app.sources.tencent import fetch_spot

logger = logging.getLogger(__name__)

# 「竞价结果」列的采样窗口（HHMM）。9:15 集合竞价开始、9:25 出开盘价，9:26 收最后一帧。
RESULT_FIRST = "0915"
RESULT_LAST = "0926"

# 只补量比的那一帧（见模块说明）。腾讯开盘前不填量比，9:26 前取到恒为 0。
RATIO_SAMPLE = "0931"

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
    result_frame = RESULT_FIRST <= hhmm <= RESULT_LAST
    ratio_frame = hhmm == RATIO_SAMPLE
    if not (result_frame or ratio_frame):
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
        if not result_frame:
            # 9:31 那一帧：**只刷量比**。必须限定在「已存在的行」上 —— 否则
            # `upsert_many` 会把没有竞价结果的票插成「只有量比、其余全空」的脏行。
            known = set(
                session.scalars(
                    select(AuctionSnapshot.code).where(AuctionSnapshot.trade_date == day)
                )
            )
            rows: list[dict] = [
                {
                    "trade_date": day,
                    "code": item["code"],
                    "volume_ratio": item["volume_ratio"],
                }
                for item in quotes
                if item["code"] in known and item["volume_ratio"] is not None
            ]
        else:
            # 已有的逐分钟序列：读回来往后追加（`upsert_many` 是整列覆盖，不读会丢掉前面几帧）
            existing = {
                code: samples
                for code, samples in session.execute(
                    select(AuctionSnapshot.code, AuctionSnapshot.samples).where(
                        AuctionSnapshot.trade_date == day
                    )
                ).all()
            }
            rows = []
            for item in quotes:
                code = item["code"]
                price = item["price"]
                pre_close = item["pre_close"]
                samples = list(existing.get(code) or [])
                # 逐分钟帧：下标见 `models.SAMPLE_*`（写读两端共用一份，别自己数位置）。
                # 后 5 列是 2026-10-12 加的（未匹配量差 / 9:20 撤单 / 封成比用）——
                # ⚠️ 老行只有前 3 个，读端按固定下标取、越界当 None。
                samples.append(
                    [
                        hhmm,
                        price,
                        item["amount"],
                        item["committee_diff"],
                        item["bid1_price"],
                        item["bid1_volume"],
                        item["ask1_price"],
                        item["ask1_volume"],
                    ]
                )
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
                        # 这一帧的量比一定是 0（开盘前腾讯不填），等 9:31 那帧补
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
