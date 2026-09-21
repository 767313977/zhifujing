"""回补涨停 / 跌停 / 炸板三池的命令行入口。

东财那三个接口只保留最近约 15 个交易日，超过就只剩空表 —— 所以这个脚本
既是「补历史」的工具，也是**漏采之后的唯一补救手段**（见 `jobs/backfill_pools.py`）。

用法::

    # 先对账：拿一天库里已有的东财数据，跟 iFinD 的取数结果比一比
    python scripts/backfill_pools.py --verify 2026-09-15

    # 补历史，每轮最多 20 天（一天 3 次 iFinD 调用），跑完会报还剩几天
    python scripts/backfill_pools.py --from 2025-09-08 --to 2026-08-27 --days 20

**一定要先跑 `--verify`**：选股问句里任何一个指标不被支持，整条查询都会静默
返回 0 行，只有在已有的东财数据上才能看出来（见模块说明第 3、5 条）。
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
from app.jobs import backfill_pools  # noqa: E402
from app.services.usage import quota_status  # noqa: E402


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期要写 YYYY-MM-DD，收到 {value}") from exc


def _run_verify(day: date) -> int:
    report = backfill_pools.verify_day(day)
    print(f"\n对账 {day}（iFinD 选股 vs 库里的东财数据）：")
    ok = True
    for name, item in report.items():
        same = item["只有 iFinD"] == [] and item["只有东财"] == []
        ok = ok and same
        mark = "一致" if same else "**不一致**"
        print(
            f"  {name:7s} iFinD={item['ifind']:4d}  东财={item['东财']:4d}  "
            f"交集={item['两边都有']:4d}  {mark}"
        )
        if item["只有 iFinD"]:
            print(f"          只有 iFinD（其中 ST {item['只有 iFinD 里含 ST']} 只）：{item['只有 iFinD']}")
        if item["只有东财"]:
            print(f"          只有东财：  {item['只有东财']}")
    print("\n结论：" + ("口径对得上，可以开跑" if ok else "有差异，先查清再跑"))
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="回补涨停三池（iFinD 选股）")
    parser.add_argument("--from", dest="start", type=_parse_date, help="起始日期（含）")
    parser.add_argument("--to", dest="end", type=_parse_date, help="结束日期（含），缺省=今天")
    parser.add_argument(
        "--days",
        type=int,
        default=20,
        help="本轮最多补几个交易日（默认 20，约 60 次调用）。0 = 不限",
    )
    parser.add_argument("--verify", type=_parse_date, help="只对账某一天，不写库")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    init_db()

    if args.verify:
        return _run_verify(args.verify)

    if not args.start:
        parser.error("要写 --from（起始日期）；或先用 --verify 对账一天")

    end = args.end or date.today()
    result = backfill_pools.backfill(
        args.start, end, max_days=args.days, settings=get_settings()
    )
    usage = quota_status()
    print(
        f"\n完成：补 {len(result['done'])} 天 / {result['calls']} 次调用 / "
        f"{result['rows']} 行 / {result['cost_seconds']}s"
    )
    if result["done"]:
        print(f"补到的交易日：{result['done'][0]} ~ {result['done'][-1]}")
    if result["remaining"]:
        print(
            f"窗口内还剩 {result['remaining']} 天待补"
            f"（约需 {result['remaining'] * 3} 次调用），再跑一次这个命令继续"
        )
    elif not result["done"]:
        print("窗口内没有缺的天，不需要补")
    if result["failed"]:
        print(f"失败/跳过 {len(result['failed'])} 天：{result['failed'][:3]}")
    print(
        f"本周期（{usage['cycle_start']} ~ {usage['cycle_end']}）iFinD 已用 "
        f"{usage['cycle_calls']} / {usage['monthly_quota']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
