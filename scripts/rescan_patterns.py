"""按**当前规则**重扫一段交易日（零 iFinD 配额）。

为什么需要它：形态判定改过多次，而 `pattern_hit` 里留着旧规则扫出来的历史行。
成绩单 / 胜率跟踪要求「同一套口径」才可比，所以改完规则要先把历史重扫一遍。
这个脚本就是把之前手动敲的那段循环固定下来：

    python scripts/rescan_patterns.py --start 2026-09-24 --end 2026-10-16

- **只重扫交易日**（以 `trade_calendar` 为准），且不越过库里有日线的最后一天；
- `scan(..., no_ifind=True)`：候选补日线只走东财 / 腾讯，**不碰 iFinD**；
- `scan()` 是「整段替换当天命中」，重复跑幂等；
- 单日失败不中断后面的日期，最后汇总失败天数并以非 0 退出。
"""

import argparse
import logging
import sys
import time
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import func, select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.jobs.scan_patterns import scan  # noqa: E402
from app.models import StockDaily, TradeCalendar  # noqa: E402

logger = logging.getLogger("rescan")


def _days(start: date, end: date | None) -> list[date]:
    with session_scope() as session:
        data_end = session.scalar(select(func.max(StockDaily.trade_date)))
        days = list(
            session.scalars(
                select(TradeCalendar.trade_date)
                .where(TradeCalendar.trade_date >= start)
                .order_by(TradeCalendar.trade_date)
            )
        )
    if data_end is None:
        return []
    return [day for day in days if day <= data_end and (end is None or day <= end)]


def main() -> int:
    parser = argparse.ArgumentParser(description="按当前规则重扫交易日（零 iFinD 配额）")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    days = _days(args.start, args.end)
    if not days:
        print("==> 区间内没有可重扫的交易日（或库里还没有日线）")
        return 1
    print(f"==> 重扫 {len(days)} 个交易日：{days[0]} ~ {days[-1]}（no_ifind=True）")

    failures = 0
    started = time.monotonic()
    for day in days:
        day_started = time.monotonic()
        try:
            result = scan(day, no_ifind=True)
            print(
                f"  {day} ok {time.monotonic() - day_started:.1f}s "
                f"codes={result.get('codes')} stale={result.get('stale')}"
            )
        except Exception as exc:  # noqa: BLE001 - 单日失败继续跑后面的日期
            failures += 1
            print(f"  {day} FAILED：{exc}")
    print(
        f"==> 完成：{len(days) - failures}/{len(days)} 成功，"
        f"用时 {time.monotonic() - started:.0f}s"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
