"""回补涨停原因历史（同花顺）。

日常采集只写当天，这个脚本用来一次补很长一段 —— 同花顺这个接口**已回溯到
2025-09-22 附近**，再往前返回 `status_code=-1`（超出覆盖范围），所以补不到 252 天
以外的日子，那些天在页面上就是空的。

零 iFinD 配额，成本与区间成正比（每个交易日 **1 次**请求），比板块回补便宜得多：
补 252 天约 252 次请求、一两分钟。

跑法（云端后台跑，别占着 ssh 会话）::

    sudo -u fupan nohup /opt/fupan/.venv/bin/python /opt/fupan/scripts/backfill_reasons.py \
        --since 2025-09-17 > /tmp/reasons_backfill.log 2>&1 &

中断了直接重跑：每个交易日是**整天替换**（先删后插），重跑只重写那一天。
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

from sqlalchemy import func, select  # noqa: E402 - 依赖上面改 sys.path

from app.config import get_settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.jobs.collect_reasons import ReasonCollector  # noqa: E402
from app.models import LimitPool, LimitReason  # noqa: E402


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期要写 YYYY-MM-DD，收到 {value}") from exc


def _coverage() -> None:
    """打印覆盖情况：涨停原因的天数 / 行数，以及涨停池有多少天可作分母。"""
    with session_scope() as session:
        reason_days, first, last, rows = session.execute(
            select(
                func.count(func.distinct(LimitReason.trade_date)),
                func.min(LimitReason.trade_date),
                func.max(LimitReason.trade_date),
                func.count(),
            )
        ).one()
        pool_days = session.scalar(
            select(func.count(func.distinct(LimitPool.trade_date))).where(
                LimitPool.pool_type == "up"
            )
        )
    if not rows:
        print("  （库里没有任何涨停原因）")
        return
    print(f"  涨停原因 {reason_days:4d} 个交易日  {first} ~ {last}  {rows} 行")
    print(f"  涨停池   {pool_days:4d} 个交易日（可作分母，差额就是没补到的）")


def main() -> int:
    parser = argparse.ArgumentParser(description="回补同花顺涨停原因")
    parser.add_argument("--since", type=_parse_date, required=True, help="起始日期（含）")
    parser.add_argument("--until", type=_parse_date, help="结束日期（含），缺省=今天")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    init_db()
    until = args.until or date.today()

    if args.since > until:
        raise SystemExit(f"--since {args.since} 晚于 --until {until}")

    print(f"回补涨停原因 {args.since} ~ {until}")
    print("回补前：")
    _coverage()

    started = time.monotonic()
    result = ReasonCollector(get_settings()).collect_range(until, start=args.since)
    print(
        f"\n完成：{result['days']} 个交易日，写入 {result['written']} 行，"
        f"{result['empty_days']} 天空返回，用时 {(time.monotonic() - started) / 60:.1f} 分钟"
    )
    print("回补后：")
    _coverage()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
