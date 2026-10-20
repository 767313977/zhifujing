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
import time
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

#: 采样窗口内**计划**采的那几刻（HHMM，升序）。见 `_frame_hhmm`。
PLANNED_MINUTES = [f"09{minute:02d}" for minute in range(15, 27)] + [RATIO_SAMPLE]

_CST = timezone(timedelta(hours=8))


def _frame_hhmm(moment: datetime) -> str | None:
    """这一趟**计划**采哪一分钟：窗口内「不超过 `moment` 的最后一刻」，窗口外返回 None。

    为什么不用实际运行时刻（2026-10-12 修）：定时任务允许晚到 90 秒
    （`misfire_grace_time=90`）。晚到时按实际时刻取「几点几分」，9:26 那次会被
    算成 0927 而跳过、9:31 那次会被算成 0932 而丢掉量比（腾讯开盘前不填量比，
    这一帧丢了当天就一直是空）。窗口内相邻两刻最多差 5 分钟，而晚到上限 90 秒，
    所以「最后一刻 ≤ 现在」总是恰好落在计划的那一分钟上。
    """
    limit = moment.strftime("%H%M")
    hit: str | None = None
    for candidate in PLANNED_MINUTES:
        if candidate > limit:
            break
        hit = candidate
    return hit


def _is_today_quote(item: dict, day: date) -> bool:
    """这一行的**行情时间**是不是 `day`。

    9:15 刚开始、或停牌的票，腾讯可能还在返回**昨天的**快照 —— 那样会把昨天全天的
    成交额当成竞价额写进库，而且从数字上看不出错（2026-10-12 修：行情时间一直解析了
    却没用上）。行情时间取不到的（个别票字段缺失）**放行**，避免误杀好数据。
    """
    raw = item.get("quote_time")
    if not raw:
        return True
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if len(digits) < 8:
        return True
    return digits[:8] == day.strftime("%Y%m%d")


def _positive(value: float | None) -> float | None:
    """价格类的清洗：**≤0 一律当缺值**。

    没有虚拟成交价的票腾讯给的是 0。直接拿去算竞价涨幅会得到 **−100%** —— 那不是
    「跌了百分之百」而是「没有数据」，既会排到升序第一，又把所在板块的平均涨幅拉下去
    （2026-10-12 修）。
    """
    if value is None or value <= 0:
        return None
    return value


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
    started = time.monotonic()
    moment = now or now_cst()
    day = moment.date()
    # 定帧用**计划**时刻，不用实际运行时刻（见 `_frame_hhmm` 的说明）
    hhmm = _frame_hhmm(moment)
    if hhmm is None:
        return 0
    # `hhmm` 一定落在 `PLANNED_MINUTES` 里（要么 None 已提前返回），所以除了 9:15–9:26
    # 那一段，剩下的就是唯一的 9:31 量比帧 —— 不用再单独判一次。
    result_frame = RESULT_FIRST <= hhmm <= RESULT_LAST
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

    # 丢掉**行情时间不是今天**的行：9:15 刚开始或停牌的票，腾讯可能还在返回昨天的
    # 快照 —— 那会把昨天全天的成交额当成竞价额写库（见 `_is_today_quote`）。
    quotes = [item for item in quotes if _is_today_quote(item, day)]
    if not quotes:
        logger.warning("竞价采样：%s %s 的行全是旧快照（行情时间不是今天），跳过", day, hhmm)
        return 0

    with session_scope() as session:
        if not result_frame:
            # 9:31 那一帧：**只刷量比 + 开盘后现价**。必须限定在「已存在的行」上 —— 否则
            # `upsert_many` 会把没有竞价结果的票插成「只有这两列、其余全空」的脏行。
            known = set(
                session.scalars(
                    select(AuctionSnapshot.code).where(AuctionSnapshot.trade_date == day)
                )
            )
            rows: list[dict] = []
            for item in quotes:
                if item["code"] not in known:
                    continue
                volume_ratio = item["volume_ratio"]
                # 量比 ≤0 当缺值（2026-10-20 修）：腾讯对**竞价无成交**的票填 0
                # （全天还一笔交易都没有，量比分母都不存在），那不是「量比真的是 0」
                # 而是「算不出」。写 0 会让这类票在量比列显示 0.00、混进量比升序的队首。
                if volume_ratio is not None and volume_ratio <= 0:
                    volume_ratio = None
                # 开盘后的现价 → 「开盘后涨幅」（它 ÷ 昨收 − 1）：看高开有没有被接住。
                # ≤0 当缺值（停牌 / 无成交时腾讯给 0，见 `_positive`）。
                post_open_price = _positive(item["price"])
                if volume_ratio is None and post_open_price is None:
                    continue
                rows.append(
                    {
                        "trade_date": day,
                        "code": item["code"],
                        "volume_ratio": volume_ratio,
                        "post_open_price": post_open_price,
                    }
                )
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
                # ≤0 当缺值：没有虚拟成交价的票腾讯给 0，直接算会得到 −100% 的竞价涨幅
                # （见 `_positive`）。样本里也一并存清洗后的值。
                price = _positive(item["price"])
                pre_close = item["pre_close"]
                samples = list(existing.get(code) or [])
                # 同一分钟重跑（晚到 / 手动触发）时**替换**那一帧，而不是再追加一条：
                # 追加会让序列里出现两条相同的 0921，而撤单率 / 封成比那种「按分钟取帧」
                # 的算法会取到错的那条。`_frame_hhmm` 用计划时刻定帧，晚到 90 秒内同一
                # 计划分钟会命中两次，所以这里必须幂等（2026-10-12 修）。
                samples = [frame for frame in samples if frame[0] != hhmm]
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
                        # 开盘价 ≤0 当缺值：竞价无成交的票腾讯给 0（约 80 只/天），
                        # 那不是「开盘价是 0」而是「没有开盘价」（2026-10-20 修）。
                        "open": _positive(item["open"]),
                        "amount": item["amount"],
                        "volume": item["volume"],
                        "turnover": item["turnover"],
                        # 竞价几帧**不写量比**：腾讯开盘前不填（返回的是 0）。写 0 会让
                        # 9:25–9:31 这段最常看的时间整列显示 0.00、「量比 ≥2」筛选全空，
                        # 而且 9:31 那帧没采到就一直是 0。真正的量比由 9:31 那帧补。
                        # （2026-10-12 修）
                        "volume_ratio": None,
                        "float_mv": item["float_mv"],
                        "total_mv": item["total_mv"],
                        # 涨跌停价 ≤0 当缺值：腾讯对**无涨跌停**的票（新股上市初期等）
                        # 填 −1 哨兵值。存 −1 会让读端「price >= limit_up」恒真，
                        # 把普通票误算成「竞价涨停」（2026-10-20 修）。
                        "limit_up": _positive(item["limit_up"]),
                        "limit_down": _positive(item["limit_down"]),
                        "samples": samples,
                    }
                )
        upsert_many(session, AuctionSnapshot, rows)

    elapsed = time.monotonic() - started
    logger.info("竞价采样 %s %s：%d 只，耗时 %.1f 秒", day, hhmm, len(rows), elapsed)
    if elapsed > 60:
        # 全市场约 110 个请求。定时任务是 `coalesce=True` + 单实例，一轮超过 60 秒就会
        # 把下一分钟那一帧挤掉（缺帧在 `samples` 里看得出来）。记一条告警便于事后对账。
        logger.warning(
            "竞价采样 %s %s 耗时 %.1f 秒（> 60 秒），下一分钟那一帧可能被跳过",
            day,
            hhmm,
            elapsed,
        )
    return len(rows)
