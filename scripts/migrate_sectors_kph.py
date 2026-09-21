"""一次性迁移：板块口径从同花顺换成开盘红（2026-09-21）。

做三件事，**顺序不能换**：

1. **清掉旧口径的行**：`sector_daily` / `sector_basic` / `sector_member` 里的
   `ths_*` 行，以及 `stock_concept` 的全部行。不清会出两个具体问题：
   - 开盘红的**行业代码与同花顺行业是同一套**（都是 881xxx），而 `sector_daily`
     主键是 `(trade_date, sector_code)`、不含 taxonomy —— 新行会直接盖掉旧行，
     库里变成两套口径按代码混在一起；
   - `stock_concept` 存的是同花顺概念名，与开盘红板块名对不上，
     题材聚合会整体落空，而页面上看起来只是「今天没有题材共振」。
2. 刷新板块清单（精选 270 + 行业 104）写进 `sector_basic`。
3. 可选：回补历史（`--backfill-days`，成本与天数成正比），并**把涨停题材一起补**
   （`--backfill-themes`）—— 两者是两张表，只补行情的话板块页的「涨停」列会全是 `—`。

顺带 `DROP TABLE stock_theme`：那张表是「打开过的个股 → 同花顺概念」的缓存，
模型已经删了，留着只是没人引用的死表。

跑法（云端后台跑，别占着 ssh 会话）::

    sudo -u fupan nohup /opt/fupan/.venv/bin/python \\
        /opt/fupan/scripts/migrate_sectors_kph.py \\
        --wipe --refresh-list --backfill-days 380 --backfill-themes \\
        > /tmp/kph_migrate.log 2>&1 &

**这个脚本只在切换那一次需要跑。** 跑完 `--wipe` 再执行一次是幂等的（旧口径的行
已经没了），但没必要；日常采集由 `jobs/collect_sectors.py` 与
`jobs/collect_themes.py` 负责。
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

from sqlalchemy import delete, func, select, text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import engine, init_db, session_scope  # noqa: E402
from app.jobs.collect_sectors import SectorCollector  # noqa: E402
from app.models import (  # noqa: E402
    SectorBasic,
    SectorDaily,
    SectorMember,
    StockConcept,
    TradeCalendar,
)
from app.sources.kaipanhong import TAXONOMIES  # noqa: E402

logger = logging.getLogger("migrate")

# 旧口径的 taxonomy 取值。写死而不是 import：那两个常量已经从代码里删掉了，
# 这个脚本要能在「新代码 + 旧数据」的组合下把旧行认出来。
LEGACY_TAXONOMIES = ("ths_industry", "ths_concept")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期要写 YYYY-MM-DD，收到 {value}") from exc


def _report(label: str) -> None:
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
        basics = session.execute(
            select(SectorBasic.taxonomy, func.count()).group_by(SectorBasic.taxonomy)
        ).all()
        concepts = session.scalar(
            select(func.count()).select_from(StockConcept)
        )
        members = session.scalar(select(func.count()).select_from(SectorMember))
    print(f"\n[{label}] sector_daily：")
    if not rows:
        print("  （空）")
    for taxonomy, days, first, last, total in rows:
        print(f"  {taxonomy:13s} {days:4d} 个交易日  {first} ~ {last}  {total} 行")
    print(f"  sector_basic：{dict(basics) or '空'}  stock_concept：{concepts} 行  "
          f"sector_member：{members} 行")


def wipe() -> None:
    """删掉旧口径的行。"""
    with session_scope() as session:
        # 成分股表没有 taxonomy 列，但存的是旧口径的板块代码，整体清掉
        print(f"  删除 sector_member：{session.execute(delete(SectorMember)).rowcount} 行")
        print(
            "  删除 sector_daily（ths_*）："
            f"{session.execute(delete(SectorDaily).where(SectorDaily.taxonomy.in_(LEGACY_TAXONOMIES))).rowcount} 行"
        )
        print(
            "  删除 sector_basic（ths_*）："
            f"{session.execute(delete(SectorBasic).where(SectorBasic.taxonomy.in_(LEGACY_TAXONOMIES))).rowcount} 行"
        )
        print(
            f"  删除 stock_concept：{session.execute(delete(StockConcept)).rowcount} 行"
            "（旧的是同花顺概念名）"
        )

        # 还可能留着 taxonomy 既不是 ths_* 也不是 kph_* 的行（以前实验写进去的）。
        # 新口径只有两个取值，其余一并清掉，免得它们混进「板块总数」里
        others = session.execute(
            delete(SectorDaily).where(SectorDaily.taxonomy.not_in(TAXONOMIES))
        ).rowcount
        others += session.execute(
            delete(SectorBasic).where(SectorBasic.taxonomy.not_in(TAXONOMIES))
        ).rowcount
        if others:
            print(f"  删除其它口径的行：{others} 行")

    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS stock_theme"))
    print("  已 DROP 死表 stock_theme")


def main() -> int:
    parser = argparse.ArgumentParser(description="板块口径迁移到开盘红")
    parser.add_argument("--wipe", action="store_true", help="清掉同花顺口径的旧行")
    parser.add_argument("--refresh-list", action="store_true", help="刷新板块清单")
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=0,
        help="回补最近 N 个自然日（0 = 不回补）。约 252 个交易日对应 380",
    )
    parser.add_argument(
        "--backfill-themes",
        action="store_true",
        help="同时按日回补涨停题材（涨停天梯 → stock_concept）",
    )
    parser.add_argument("--until", type=_parse_date, help="回补截止日，缺省=今天")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    init_db()
    _report("迁移前")

    if args.wipe:
        print("\n清理旧口径的行：")
        wipe()

    collector = SectorCollector(get_settings())
    until = args.until or date.today()

    if args.refresh_list:
        print(f"\n刷新板块清单（以 {until} 为准）：")
        counts = collector.refresh_sector_list(until)
        print(f"  {counts}")

    if args.backfill_days > 0:
        print(f"\n回补最近 {args.backfill_days} 个自然日（截止 {until}）：")
        started = time.monotonic()
        result = collector.collect_range(until, days=args.backfill_days)
        print(
            f"  {result['days']} 个交易日，{result['written']} 行，"
            f"用时 {(time.monotonic() - started) / 60:.1f} 分钟"
        )

    if args.backfill_themes:
        print("\n回补涨停题材（每日 1 次请求）：")
        from app.jobs.collect_themes import ThemeCollector

        themes = ThemeCollector(get_settings())
        with session_scope() as session:
            days = list(
                session.scalars(
                    select(TradeCalendar.trade_date)
                    .where(TradeCalendar.trade_date <= until)
                    .order_by(TradeCalendar.trade_date.desc())
                    .limit(max(args.backfill_days, 260))
                )
            )
        total = 0
        empty: list[date] = []
        for index, day in enumerate(sorted(days), 1):
            written = themes.collect(day)
            total += written
            if written == 0:
                empty.append(day)
            if index % 20 == 0:
                logger.info("涨停题材进度 %d/%d", index, len(days))
        print(f"  {len(days)} 个交易日，共 {total} 行")
        if empty:
            # 空行不一定是故障：那几天可能真的没有涨停股，也可能接口没数据。
            # 如实列出来，让人自己判断，而不是默默跳过
            print(f"  ⚠ 有 {len(empty)} 天一行都没取到：{[str(d) for d in empty[:10]]} …")

    _report("迁移后")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
