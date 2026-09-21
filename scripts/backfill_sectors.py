"""深度回补板块历史（开盘红）。

日常采集只写当天，这个脚本用来一次补很长一段 —— 把板块行情补到与其它表对齐的
250 个交易日。

**成本与区间成正比**（每天两个口径各翻 6 页），与旧版完全不同：旧版用的是同花顺
板块指数接口，它每次都返回板块的全历史再按日期切，所以「补一年」和「补两周」
都是每个板块一次调用。换成开盘红之后，补 250 天 ≈ 250 × 14 ≈ 3500 次请求，
一轮十几分钟（游客接口，没有配额，但会按 3 次/秒限速）。

跑法（云端后台跑，别占着 ssh 会话）::

    sudo -u fupan nohup /opt/fupan/.venv/bin/python /opt/fupan/scripts/backfill_sectors.py \
        --since 2025-09-08 > /tmp/sectors_backfill.log 2>&1 &

中断了直接重跑：每个交易日是**整天替换**（先删后插），所以重跑只会重写那一天，
不会留半截数据，也不会产生重复行。
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

from app.config import get_settings  # noqa: E402 - 依赖上面改 sys.path
from app.db import init_db, session_scope  # noqa: E402
from app.jobs.collect_sectors import SectorCollector  # noqa: E402
from app.models import SectorDaily  # noqa: E402

from sqlalchemy import func, select  # noqa: E402


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期要写 YYYY-MM-DD，收到 {value}") from exc


def _coverage() -> None:
    with session_scope() as session:
        rows = session.execute(
            select(
                SectorDaily.taxonomy,
                func.count(func.distinct(SectorDaily.trade_date)),
                func.min(SectorDaily.trade_date),
                func.max(SectorDaily.trade_date),
                func.count(),
            ).group_by(SectorDaily.taxonomy)
        ).all()
    if not rows:
        print("  （库里没有任何板块行）")
    for taxonomy, days, first, last, total in rows:
        print(f"  {taxonomy:13s} {days:4d} 个交易日  {first} ~ {last}  {total} 行")


def main() -> int:
    parser = argparse.ArgumentParser(description="回补开盘红板块历史")
    parser.add_argument("--since", type=_parse_date, required=True, help="起始日期（含）")
    parser.add_argument("--until", type=_parse_date, help="结束日期（含），缺省=今天")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    init_db()
    until = args.until or date.today()

    if args.since > until:
        raise SystemExit(f"--since {args.since} 晚于 --until {until}")

    print(f"回补开盘红板块行情 {args.since} ~ {until}")
    print("回补前：")
    _coverage()

    started = time.monotonic()
    result = SectorCollector(get_settings()).collect_range(until, start=args.since)
    print(
        f"\n完成：{result['days']} 个交易日，写入 {result['written']} 行，"
        f"用时 {(time.monotonic() - started) / 60:.1f} 分钟"
    )
    print("回补后：")
    _coverage()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
