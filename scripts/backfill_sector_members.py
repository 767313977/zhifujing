"""回补 `sector_member`（板块成分股）的历史快照。

## 为什么需要它

`sector_member` 只有两条写入路径：当晚 22:00 的 `collect_all_members`，和打开板块时
现取（`api/sector._board_members`）。所以 2026-09-28 之前的日子基本是空的（只有零星
几个板块被「打开过」）。想从板块强度落到个股、并且回测，就必须有**逐日、全板块**的
成分股 —— 拿今天的成分回看历史是前视偏差（调入调出的票会被当成「一直在里面」）。

开盘红的 `ZhiShuStockList_W8` 支持历史日期，且返回的是**当时**的成分：实测 2026-03-02
的 801001 是 970 只、2026-10-23 是 1136 只 —— 说明是真历史，不是当前快照。

## 用法

    python scripts/backfill_sector_members.py --days 60        # 最近 60 个交易日
    python scripts/backfill_sector_members.py --start 2026-07-24 --end 2026-09-30
    python scripts/backfill_sector_members.py --days 60 --dry-run

⚠️ 只补**缺失**的板块（当天已有成分股的板块跳过），所以随时可以中断再跑 —— 重跑只会
补剩下的。实测速度 0.38 秒/板块，一天（374 个板块）约 2.4 分钟。
"""

import argparse
import logging
import sys
import time
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
BACKEND = SCRIPTS.parent / "backend"
for path in (str(BACKEND), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

from sqlalchemy import func, select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.jobs.collect_sectors import SectorCollector  # noqa: E402
from app.models import SectorBasic, SectorMember, StockDaily, TradeCalendar  # noqa: E402

logger = logging.getLogger("backfill")


def _target_days(args: argparse.Namespace) -> list[date]:
    """要处理哪几个交易日（升序）。`--start` 优先于 `--days`。

    ⚠️ 上限默认取 `max(stock_daily.trade_date)` 而不是「今天」：`trade_calendar` 里
    存着**全年的预告日历**（本地实测能取到 2026-12-31），直接 `limit(days)` 会抓到
    一堆未来日期。用实际行情截止日兜底。
    """
    with session_scope() as session:
        end = args.end or session.scalar(select(func.max(StockDaily.trade_date)))
        if end is None:
            return []
        statement = (
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date <= end)
            .order_by(TradeCalendar.trade_date.desc())
        )
        if args.start:
            statement = statement.where(TradeCalendar.trade_date >= args.start)
            return sorted(session.scalars(statement))
        return sorted(session.scalars(statement.limit(args.days)))


def _missing_boards(day: date, total: int) -> int:
    with session_scope() as session:
        have = (
            session.scalar(
                select(func.count(func.distinct(SectorMember.sector_code))).where(
                    SectorMember.trade_date == day
                )
            )
            or 0
        )
    return max(total - have, 0)


def main() -> int:
    parser = argparse.ArgumentParser(description="回补 sector_member 的历史成分股")
    parser.add_argument("--days", type=int, default=60, help="最近多少个交易日（默认 60）")
    parser.add_argument("--start", type=date.fromisoformat, help="起始日（含），优先于 --days")
    parser.add_argument("--end", type=date.fromisoformat, help="截止日（含），缺省 = 最新")
    parser.add_argument("--dry-run", action="store_true", help="只打印缺口，不抓取")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    days = _target_days(args)
    if not days:
        logger.warning("没有可用的交易日，先确认 trade_calendar 有数据")
        return 1

    with session_scope() as session:
        total = session.scalar(select(func.count()).select_from(SectorBasic)) or 0
    logger.info(
        "目标 %s ~ %s 共 %d 个交易日，板块清单 %d 个%s",
        days[0],
        days[-1],
        len(days),
        total,
        "（dry-run）" if args.dry_run else "",
    )

    collector = SectorCollector()
    started = time.monotonic()
    written = failed = empty = 0
    for index, day in enumerate(days, 1):
        gap = _missing_boards(day, total)
        if gap == 0:
            logger.info("[%d/%d] %s 已完整，跳过", index, len(days), day)
            continue
        if args.dry_run:
            logger.info("[%d/%d] %s 缺 %d 个板块", index, len(days), day, gap)
            continue

        day_start = time.monotonic()
        result = collector.collect_all_members(day)
        written += result.get("written", 0)
        failed += result.get("failed", 0)
        logger.info(
            "[%d/%d] %s 补 %d 个板块 / %d 行，失败 %d，用时 %.0fs",
            index,
            len(days),
            day,
            result.get("boards", 0),
            result.get("written", 0),
            result.get("failed", 0),
            time.monotonic() - day_start,
        )
        if result.get("boards") and not result.get("written"):
            # 一条都没写：要么超出开盘红的历史保留期，要么当天名单缺失
            empty += 1
            logger.warning("%s 一个板块都没取到，可能已超出历史保留期", day)

    logger.info(
        "完成：%d 天，写入 %d 行，失败 %d 个板块，空天 %d，总用时 %.0fs",
        len(days),
        written,
        failed,
        empty,
        time.monotonic() - started,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
