"""全市场日线的命令行入口：首次建库与手工补采。

日常的增量采集已经挂在调度器里（交易日 17:30 那轮自动跑），这个脚本只用于
两件事：

- **首次建库**：250 个交易日 × 每天 14 次调用 ≈ 3500 次，一轮 5000 的月度配额
  都不够，所以既能命令行跑、又能 `--days` 分批
- **手工补采**：某天采集失败或想强制重刷时用

**分批补历史的正确用法**：窗口是**从旧到新**走的，`--days N` 卡的是
「本轮最多补 N 天」，所以每跑一轮就往前推进一段，跑完打印还剩几天待补 ——
看到「还有 N 天待补」就再跑一次，直到打出「没有缺的天」。已经补过的天会被
直接跳过（只查库、不花调用），所以重复跑、中途断掉重跑都不会浪费配额。

用法::

    python scripts/collect_kline.py            # 增量：最近十几个日历日
    python scripts/collect_kline.py --full     # 检查整段历史，缺什么补什么
    python scripts/collect_kline.py --full --days 20   # 同上，但本轮最多补 20 天
    python scripts/collect_kline.py --pool     # 只重建股票池
"""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import get_settings  # noqa: E402 - 依赖上面改 sys.path
from app.db import init_db  # noqa: E402
from app.jobs.collect_kline import KlineCollector  # noqa: E402
from app.jobs.collect_universe import UniverseCollector, load_codes  # noqa: E402
from app.services.usage import quota_status  # noqa: E402


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期要写 YYYY-MM-DD，收到 {value}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="采集全市场日线")
    parser.add_argument("--full", action="store_true", help="全量建库（250 个交易日）")
    parser.add_argument("--pool", action="store_true", help="只重建股票池")
    parser.add_argument("--date", type=_parse_date, help="截止交易日，缺省取最近交易日")
    parser.add_argument("--force-pool", action="store_true", help="池子没过期也强制重建")
    parser.add_argument(
        "--days",
        type=int,
        default=0,
        help="这轮最多补几个交易日（0 = 不限）。建库时用来按配额分批",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db()
    settings = get_settings()

    if args.pool or args.force_pool:
        print("重建股票池 ->", UniverseCollector(settings).collect(args.date, force=True))
    if args.pool:
        return 0

    codes = load_codes()
    if not codes:
        print("股票池为空。先跑一次：python scripts/collect_kline.py --pool")
        return 1
    print(f"股票池 {len(codes)} 只")
    if args.full:
        print("整段历史要 3500 次上下，建议配 --days 分批跑（每天 14 次调用）")

    result = KlineCollector(settings).collect(
        args.date, full=args.full, max_days=args.days
    )
    if result.get("status") == "skipped":
        print("跳过：", result.get("reason"))
        return 0

    usage = quota_status()
    print(
        f"\n完成：采 {len(result['fetched_days'])} 天（跳过 {result['skipped_days']} 天）/ "
        f"{result['calls']} 次调用 / {result['rows']} 行 / "
        f"清理 {result['pruned']} 行 / {result['cost_seconds']}s"
    )
    if result["fetched_days"]:
        print("补到的交易日：", ", ".join(result["fetched_days"]))
    if result["pending_days"]:
        # 分批跑的时候，这一行就是「还要不要再跑一轮」的依据
        print(
            f"窗口内还有 {result['pending_days']} 天待补"
            f"（约需 {result['pending_days'] * 14} 次调用），再跑一次这个命令继续"
        )
    elif not result["fetched_days"]:
        print("窗口内没有缺的天，不需要补")
    # 键名是 `failed` 而不是 `failed_days` —— 2026-09-22 改名（那边顺手把结果字典
    # 里的名字统一了），这个脚本当时没跟着改，`result["failed_days"]` 会直接 KeyError
    if result["failed"]:
        print(f"失败 {len(result['failed'])} 天：{result['failed'][:3]}")
    print(
        f"本周期（{usage['cycle_start']} ~ {usage['cycle_end']}）iFinD 已用 "
        f"{usage['cycle_calls']} / {usage['monthly_quota']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
