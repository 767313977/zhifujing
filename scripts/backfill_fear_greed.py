"""回补 A 股恐惧贪婪指数的六项原始指标（**零外部请求、零 iFinD 配额**）。

日常由尾部链的 `_collect_fear_greed` 自动重算最近 30 个交易日；这个脚本用来一次性
补很长一段 —— 把恐贪读数补到与 `stock_daily` 的历史（≈250 个交易日）对齐。

**成本与区间成正比，但很便宜**：全部读站内库表（指数日线 / 情绪表 / 两融 / 个股日线），
不发任何外部请求。一条 GROUP BY 扫 `stock_daily` 是主要开销，380 个自然日大约几秒。

跑法::

    python scripts/backfill_fear_greed.py --days 380

中断了直接重跑：每一天是**整行替换**（`upsert_many` 幂等），重跑只会重写那一天。
"""

import argparse
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.db import init_db, session_scope  # noqa: E402 - 依赖上面改 sys.path
from app.jobs import collect_fear_greed as cfg  # noqa: E402
from app.models import FearGreedDaily  # noqa: E402

from sqlalchemy import func, select  # noqa: E402


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期要写 YYYY-MM-DD，收到 {value}") from exc


def _coverage() -> None:
    """打印六项各自有几个非空值 —— 补出来是「六项都齐」还是「只有两项有数」，一眼能看。"""
    with session_scope() as session:
        row = session.execute(
            select(
                func.count(),
                func.min(FearGreedDaily.trade_date),
                func.max(FearGreedDaily.trade_date),
                func.count(FearGreedDaily.volatility),
                func.count(FearGreedDaily.rsi14),
                func.count(FearGreedDaily.turnover_ratio),
                func.count(FearGreedDaily.margin_chg),
                func.count(FearGreedDaily.breadth),
                func.count(FearGreedDaily.limit_strength),
            )
        ).one()
    total, first, last = row[0], row[1], row[2]
    if not total:
        print("  （库里没有任何恐贪行）")
        return
    print(f"  共 {total} 个交易日  {first} ~ {last}")
    labels = ("波动率", "RSI", "相对换手率", "融资融券", "市场宽度", "涨跌停强度")
    counts = row[3:]
    print("  非空项：" + "  ".join(f"{name} {count}" for name, count in zip(labels, counts)))


def main() -> int:
    parser = argparse.ArgumentParser(description="回补 A 股恐惧贪婪指数原始指标")
    parser.add_argument(
        "--days",
        type=int,
        default=cfg.DEFAULT_BACKFILL_DAYS,
        help=f"回看多少个自然日（默认 {cfg.DEFAULT_BACKFILL_DAYS} ≈ 250 个交易日）",
    )
    parser.add_argument("--since", type=_parse_date, help="起始日期（含）；给了它就忽略 --days")
    parser.add_argument("--until", type=_parse_date, help="结束日期（含），缺省=最近交易日")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    init_db()

    end = args.until or cfg.latest_trade_date()
    if end is None:
        raise SystemExit("市场情绪表为空，无法确定交易日范围")
    start = args.since or end - timedelta(days=args.days)
    if start > end:
        raise SystemExit(f"起始 {start} 晚于结束 {end}")

    print(f"回补恐贪指数 {start} ~ {end}")
    print("回补前：")
    _coverage()

    started = time.monotonic()
    written = cfg.collect_range(start, end)
    print(f"\n完成：写入 {written} 天，用时 {time.monotonic() - started:.1f} 秒")
    print("回补后：")
    _coverage()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
