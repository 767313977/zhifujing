"""回补 ETF 日线（通达信盘后包，零 iFinD 配额）。

一个交易日一个包（约 2.7 MB），限速 1 个/秒。官网保留 ≥ 3 年（2023-01-03 实测可下），
默认只补最近一年 —— 见指令文件「已拍板」：`--since 2025-10-01`。

用法：

    python scripts/backfill_etf_bar.py                 # 默认 --since 2025-10-01
    python scripts/backfill_etf_bar.py --since 2024-01-01
    python scripts/backfill_etf_bar.py --until 2025-12-31 --refresh   # 已有也重下

- **可断点续跑**：某天 `etf_bar` 已有 ≥ `SKIP_ROWS` 行就跳过（当日正常 1300~1700 行）；
- **非交易日 / 超出保留期**：盘后包 404，记一行日志继续，不算失败；
- **不要在这里刷 `etf_basic` 的基金类型**：那只用 akshare 调一次就够，
  脚本结束时统一跑 `refresh_basic(latest)`，不用 240 多次重复请求。
"""

import argparse
import logging
import sys
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import func, select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.jobs.collect_etf_bar import refresh, refresh_basic  # noqa: E402
from app.models import EtfBar  # noqa: E402
from app.sources.tdx import TdxError, TdxUnavailable  # noqa: E402

logger = logging.getLogger("backfill_etf_bar")

#: 某天已有这么多行就认为补过了（正常日 1300~1700，碎片/缺失日远低于它）
SKIP_ROWS = 1000

#: 两次下载之间的间隔（官网是静态文件，1 个/秒足够保守）
SLEEP_SEC = 1.0


def _existing(day: date) -> int:
    with session_scope() as session:
        return int(
            session.scalar(
                select(func.count()).select_from(EtfBar).where(EtfBar.trade_date == day)
            )
            or 0
        )


def _monthly() -> dict[str, tuple[int, int]]:
    with session_scope() as session:
        rows = session.execute(
            select(
                func.strftime("%Y-%m", EtfBar.trade_date),
                func.count(func.distinct(EtfBar.trade_date)),
                func.count(),
            ).group_by(func.strftime("%Y-%m", EtfBar.trade_date))
        ).all()
    return {month: (int(days), int(rows)) for month, days, rows in rows}


def main() -> int:
    parser = argparse.ArgumentParser(description="回补 ETF 日线（通达信盘后包）")
    parser.add_argument("--since", default="2025-10-01", help="起始日（含），默认 2025-10-01")
    parser.add_argument("--until", default=None, help="截止日（含），缺省=今天")
    parser.add_argument("--refresh", action="store_true", help="已有行也重新下载")
    parser.add_argument("--sleep", type=float, default=SLEEP_SEC, help="两次下载的间隔秒数")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    since = date.fromisoformat(args.since)
    until = date.fromisoformat(args.until) if args.until else date.today()

    done = skipped = missing = failed = 0
    monthly: Counter[str] = Counter()
    last_day: date | None = None
    cursor = since
    while cursor <= until:
        if cursor.weekday() >= 5:
            cursor += timedelta(days=1)
            continue
        have = _existing(cursor)
        if have >= SKIP_ROWS and not args.refresh:
            skipped += 1
            cursor += timedelta(days=1)
            continue
        try:
            written = refresh(cursor)
        except TdxUnavailable:
            missing += 1
            logger.info("%s 没有盘后包（非交易日 / 未发布 / 超保留期），跳过", cursor)
        except TdxError as exc:
            failed += 1
            logger.warning("%s 盘后包有问题：%s", cursor, exc)
        else:
            done += 1
            last_day = cursor
            monthly[cursor.strftime("%Y-%m")] += 1
            logger.info("%s 写入 %d 只", cursor, written)
        time.sleep(max(args.sleep, 0))
        cursor += timedelta(days=1)

    print()
    print(f"完成：新补 {done} 天、已有跳过 {skipped} 天、无包 {missing} 天、失败 {failed} 天")
    print("每月补到的交易日数：")
    for month in sorted(monthly):
        print(f"  {month}: {monthly[month]}")
    print("库里每月行数（月份: 交易日数 / 行数）：")
    for month, (days, rows) in sorted(_monthly().items()):
        print(f"  {month}: {days} 天 / {rows} 行")

    # 基金类型一天调一次就够；回补循环里不要每天下一次
    try:
        if last_day is not None:
            refresh_basic(last_day)
            print(f"已刷新 etf_basic（{last_day} 的名称 / 类型 / T+0）")
    except Exception:  # noqa: BLE001 - 类型是增强信息，失败不影响日线
        logger.warning("刷新 etf_basic 失败，下次采集会再补", exc_info=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
