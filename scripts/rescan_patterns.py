"""用**当前**的形态口径重扫历史命中（`pattern_hit`）。

## 为什么需要它

2026-10-28 第六轮体检改了两个会动形态结果的口径：

- 涨停门槛从「全市场 9.5%」改成按板块分档（主板 9.5 / 创业板·科创板 19.5 / 北交所 29.5），
  影响 `limit_surge_flat`、`shrink_limit_up`；
- 「洗后可盯」的箱体上沿 `wash_high` 改成不含当日，`broke` / `gone` 重新可达。

而 `pattern_hit` 是**每天收盘后按当时的规则**落库的 —— 历史命中仍是旧口径，形态成绩单
把新旧混在一起统计，胜率就说不清是哪一套规则的。这个脚本把指定区间用现在的代码重算
一遍（`scan()` 本来就按日整段替换，幂等，跑几次结果一样）。

## 用法

    python scripts/rescan_patterns.py --days 60
    python scripts/rescan_patterns.py --start 2026-07-01 --end 2026-10-28
    python scripts/rescan_patterns.py --days 60 --dry-run

`--days` 与 `--start` **必须给一个**：不给就报错，避免手一抖把全部历史（270+ 个交易日）
重扫一遍。`--end` 只配合 `--start` 用。

## 两点注意

- **零 iFinD 配额**：全程 `no_ifind=True`，候选补日线只走东财 / 腾讯。重扫是维护操作，
  不该为此消耗当天的额度（东财 / 腾讯那两级仍可能发请求，但都不占配额）。
- **会改写 `pattern_hit`，以及那几天的 `collect_log(task=patterns)`**。只扫「交易日历里
  有的、且不晚于最新日线」的日子；`--dry-run` 只打印清单与现有命中数，不写库。

（2026-10-28 增强：在原有 `--start/--end` 基础上加了 `--days`、`--dry-run` 与「旧命中 →
新命中」的逐日对比，原参数与零配额语义不变。）
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
from app.jobs.scan_patterns import scan  # noqa: E402
from app.models import PatternHit, StockDaily, TradeCalendar  # noqa: E402

logger = logging.getLogger("rescan")


def _target_days(args: argparse.Namespace) -> list[date]:
    """要重扫的交易日（升序）。

    上限取 `max(stock_daily.trade_date)` 而不是 `date.today()`：交易日历预置到了年底，
    按今天算会把还没有日线的未来日也排进来（那些日子 `scan()` 会直接抛错）。
    """
    with session_scope() as session:
        latest = session.scalar(select(func.max(StockDaily.trade_date)))
        if latest is None:
            return []
        statement = select(TradeCalendar.trade_date).where(
            TradeCalendar.trade_date <= latest
        )
        if args.start:
            statement = statement.where(TradeCalendar.trade_date >= args.start)
        if args.end:
            statement = statement.where(TradeCalendar.trade_date <= min(args.end, latest))
        statement = statement.order_by(TradeCalendar.trade_date)
        days = list(session.scalars(statement))
    if args.days and not args.start:
        days = days[-args.days :]
    return days


def main() -> int:
    parser = argparse.ArgumentParser(description="用当前口径重扫历史形态命中")
    parser.add_argument("--days", type=int, help="最近 N 个交易日（与 --start 二选一）")
    parser.add_argument("--start", type=date.fromisoformat, help="起始交易日 YYYY-MM-DD")
    parser.add_argument("--end", type=date.fromisoformat, help="结束交易日 YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="只打印清单与现有命中数，不写库")
    parser.add_argument("--verbose", action="store_true", help="打印扫描器的 INFO 日志")
    args = parser.parse_args()
    # 必须显式给区间：不加这道校验时「不带参数」会扫全部历史（270+ 天），
    # 一次误操作就把整张 pattern_hit 重写一遍，代价太大。
    if not args.start and not args.days:
        parser.error("请给 --days N 或 --start YYYY-MM-DD（二选一，避免误扫全部历史）")

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    days = _target_days(args)
    if not days:
        print("没有可重扫的交易日（检查 --start/--end 与日线覆盖）")
        return 1

    print(f"准备重扫 {len(days)} 个交易日：{days[0]} ~ {days[-1]}")
    # 逐日对比：旧命中条数 → 新命中条数，这样一眼能看出口径改动的影响面
    failed: list[tuple[date, str]] = []
    changed: list[tuple[date, int, int]] = []
    started = time.monotonic()

    for index, day in enumerate(days, 1):
        with session_scope() as session:
            old = session.scalar(
                select(func.count())
                .select_from(PatternHit)
                .where(PatternHit.trade_date == day)
            )
        if args.dry_run:
            print(f"[{index}/{len(days)}] {day} 现有命中 {old} 条（dry-run，不写库）")
            continue
        try:
            result = scan(day, no_ifind=True)
        except Exception as exc:  # noqa: BLE001 - 单日失败不该中断整批
            logger.warning("%s 重扫失败：%s", day, exc)
            failed.append((day, str(exc)))
            print(f"[{index}/{len(days)}] {day} 失败：{exc}")
            continue
        new = result["rows"]
        changed.append((day, old, new))
        print(
            f"[{index}/{len(days)}] {day} 命中 {old} → {new} 条"
            f"（{result['codes']} 只 / {result['cost_seconds']}s）"
        )

    if args.dry_run:
        return 0

    cost = time.monotonic() - started
    print(f"\n完成：{len(changed)} 天成功、{len(failed)} 天失败，总用时 {cost:.1f}s")
    if changed:
        delta = sum(new - old for _, old, new in changed)
        print(f"命中总数 {sum(old for _, old, _ in changed)} → {sum(new for _, _, new in changed)}（{delta:+d}）")
        biggest = sorted(changed, key=lambda item: item[2] - item[1])[:5]
        for day, old, new in biggest:
            print(f"  变动最大：{day} {old} → {new}（{new - old:+d}）")
    if failed:
        print("失败的日子（可重跑同一区间，已成功的会被整段替换，幂等）：")
        for day, message in failed:
            print(f"  {day} {message}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
