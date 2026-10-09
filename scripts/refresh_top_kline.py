"""把「形态选股」评分前 N 只的日线补到**最近交易日**（默认 50，与页面同一口径）。

形态页那张 K 线读的就是本地 `stock_daily`：某只的最后一根若早于最近交易日，
图上**最新几天就是断的**。这条脚本补的就是这个 —— 走东财 → 腾讯为主（都免费），
iFinD 只在两级都不可用时兜底，所以**常态零 iFinD 配额**，可以每天跑。

站点自身的每日链路（`scheduler._run_tail` 里的 `_backfill_hit_kline`）已经内置了
这一步；这个脚本是给**本机**用的（本机库常年落后于线上，且没有常驻调度器）。

    python scripts/refresh_top_kline.py                     # 库里最新命中日的前 50 只
    python scripts/refresh_top_kline.py --top 100           # 取前 100 只
    python scripts/refresh_top_kline.py --date 2026-09-28   # 指定命中日

口径与页面完全一致（`collect_dde.top_hit_codes`：按票归并取最高分、降序取前 N），
所以补的就是你屏幕上看到的那批票。
"""

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(
    0,
    os.environ.get("FUPAN_BACKEND", str(Path(__file__).resolve().parents[1] / "backend")),
)

from app.jobs.scan_patterns import backfill_top_kline  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="补齐形态选股前 N 只的日线")
    parser.add_argument("--top", type=int, default=50, help="取评分前多少只（默认 50）")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        help="命中的那一天（YYYY-MM-DD）；不给就用库里最新的那天",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = backfill_top_kline(args.date, limit=args.top)
    print(
        f"命中 {result.get('codes', 0)} 只 / 需补 {result.get('pending', 0)} 只 → "
        f"补 {result.get('synced', 0)} 只（东财 {result.get('via_eastmoney', 0)} / "
        f"腾讯 {result.get('via_tencent', 0)} / iFinD {result.get('via_ifind', 0)}，"
        f"失败 {result.get('failed', 0)}），目标日 {result.get('want')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
