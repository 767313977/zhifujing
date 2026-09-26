"""把个股 DDE（`5日DDE` + 单日主力净流入）的历史**按池子**补进 `stock_dde`。

## 为什么现在补得回来了

`scan_dde` 那条全市场扫描的路**与请求日期无关、永远返回最新**（所以只能逐日累积）；
而个股页这条（`get_stock_performance`）能按**日期区间**取历史 —— 2026-09-26 探针实测：
`600519 2025年4月1日至2025年6月30日` 回 91 行、日期正好落在区间内。所以历史补得回来。

## 单次上限是「100 行，按日历天」

⚠️ 超了**不报错**，而是「以下为部分数据」+ 抽样/截断（实测 303 个日历天 → 回 100 行、
跨满整个窗口、末行就是窗口起点 —— 不是取前 100 行，是**抽样**，中间会有洞）。
所以：每个区段控制在 `--segment-days`（默认 90）个日历天以内，并且**检查截断提示**，
命中就把这一段切半重来（多花 1 次调用，换数据不断档）。

## 默认只写库里没有的日期

⚠️ **不覆盖已有值**（`fill_only`，要覆盖加 `--refresh`）。原因：全市场扫描那条路给的是
**精确值**（实测 000001 2026-09-24 的 5日DDE = 322197361.17），而按区间取回的历史行
**来源会取整**（同一天给 322200000.0）—— 拿取整值把精确值盖掉是净损失。

## 成本（2026-09-26 实价，附探针结论）

| 目标历史 | 每次/只 | 全池 5288 只 | 占一个周期（7000） |
| --- | --- | --- | --- |
| 近 60 个交易日（个股页那一栏的默认窗口） | 1 | 5288 | 0.76 |
| 近 250 个交易日（接口上限） | 4 | ~21,000 | ~3 |
| 全窗口 501 个交易日 | 8~9 | ~45,000 | 6~7 |

## 配额闸门

每天的基础采集只花 300~500 次（实测 09-22~09-24 是 264 / 289 / 482），所以本脚本
**默认留 800 次底**（`--keep`）：剩余配额掉到它就停，已经补好的不会重抓，下次接着跑。
**不会**把当天的采集顶停 —— 这条比补历史重要得多。

## 用法

    python scripts/backfill_dde.py --stats               # 只看现状与配额
    python scripts/backfill_dde.py                       # 补近 60 个交易日（默认）
    python scripts/backfill_dde.py --days 250            # 补近 250 个交易日
    python scripts/backfill_dde.py --limit 10 --workers 2 # 冒烟
    python scripts/backfill_dde.py --codes 600519,000001
    python scripts/backfill_dde.py --refresh             # 已有的也重抓
    python scripts/backfill_dde.py --keep 1500           # 更保守的底
"""

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import func, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.jobs.collect_dde import collect_stock_dde_range  # noqa: E402
from app.models import StockDde, TradeCalendar  # noqa: E402
from app.services import usage  # noqa: E402
from app.sources.ifind import IfindError  # noqa: E402

logger = logging.getLogger("backfill_dde")

# 一段最多多少**日历天**。上限是来源的 100 行，留 10 行余量
_SEGMENT_DAYS = 90

# 切到多小就不再切了（再小也拿不到数据的话，就是这只票那段本来没有）
_MIN_SEGMENT_DAYS = 20


def _trade_days(days: int) -> list[date]:
    """最近 `days` 个交易日，升序。"""
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(TradeCalendar.trade_date)
                .where(TradeCalendar.trade_date <= date.today())
                .order_by(TradeCalendar.trade_date.desc())
                .limit(days)
            )
        )
    return sorted(rows)


def _coverage(codes: list[str], start: date, end: date) -> dict[str, int]:
    """每只票在 `[start, end]` 里已有多少行 DDE。"""
    with session_scope() as session:
        rows = session.execute(
            select(StockDde.code, func.count())
            .where(StockDde.trade_date >= start, StockDde.trade_date <= end)
            .group_by(StockDde.code)
        ).all()
    return {code: count for code, count in rows if code in set(codes)}


def _segments(start: date, end: date, size: int) -> list[tuple[date, date]]:
    """把 `[start, end]` 切成若干段（每段 ≤ `size` 个日历天），升序。"""
    out: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=size - 1), end)
        out.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return out


def _fetch_one(
    code: str, segments: list[tuple[date, date]], *, fill_only: bool
) -> tuple[int, int]:
    """抓一只票的所有区段。返回（写入行数, 调用次数）。

    某段被来源截断（「以下为部分数据」）就把它切半重来 —— 抽样出来的行中间有洞，
    宁可贵一次调用也不要写进去。
    """
    written = calls = 0
    queue = list(segments)
    while queue:
        seg_start, seg_end = queue.pop(0)
        calls += 1
        rows, truncated = collect_stock_dde_range(
            code, seg_start, seg_end, fill_only=fill_only
        )
        written += rows
        span = (seg_end - seg_start).days + 1
        if truncated and span > _MIN_SEGMENT_DAYS:
            mid = seg_start + timedelta(days=span // 2)
            logger.warning(
                "%s 的 %s~%s 被截断（%d 天），切成两段重来", code, seg_start, seg_end, span
            )
            queue.insert(0, (mid + timedelta(days=1), seg_end))
            queue.insert(0, (seg_start, mid))
    return written, calls


def _ordered_codes() -> list[str]:
    """要补的票的**优先级顺序**：自选股 → 日均成交额降序 → 其余。

    为什么要排序：这个回补是**分几个周期慢慢做**的（配额与每日采集共享），所以前几轮
    只覆盖一部分票。而 `load_codes()` 是按代码顺序给的 —— 那样第一轮补的全是 000/001 段，
    你天天看的 600519 要等到最后一轮才有历史（2026-09-26 实测：首轮 750 只全落在 000/001）。
    按「自选 + 流动性」排，先看的先有。
    """
    from app.jobs.collect_universe import load_codes
    from app.models import StockUniverse, Watchlist

    codes = load_codes()
    with session_scope() as session:
        watch = set(session.scalars(select(Watchlist.code)))
        amounts = {
            code: (amount or 0.0)
            for code, amount in session.execute(
                select(StockUniverse.code, StockUniverse.avg_amount)
            ).all()
        }
    return sorted(codes, key=lambda code: (code not in watch, -amounts.get(code, 0.0)))


def main() -> int:
    parser = argparse.ArgumentParser(description="按日期区间回补个股 DDE（花 iFinD 配额）")
    parser.add_argument("--days", type=int, default=60, help="往回补多少个交易日（默认 60）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 只（0 = 全部）")
    parser.add_argument("--codes", default="", help="只处理这些代码，逗号分隔")
    parser.add_argument("--refresh", action="store_true", help="已有的也重抓")
    parser.add_argument("--stats", action="store_true", help="只看现状，不发请求")
    parser.add_argument(
        "--keep", type=int, default=800, help="配额闸门：本周期剩余调用掉到这个数就停（默认 800）"
    )
    parser.add_argument(
        "--segment-days", type=int, default=_SEGMENT_DAYS, help="每段的日历天数（默认 90）"
    )
    parser.add_argument("--workers", type=int, default=4, help="并发线程数（iFinD 限 4/秒）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()

    trade_days = _trade_days(args.days)
    if not trade_days:
        print("交易日历是空的，先跑采集")
        return 1
    start, end = trade_days[0], trade_days[-1]
    segments = _segments(start, end, args.segment_days)

    status = usage.quota_status()
    print(
        f"周期 {status['cycle_start']} ~ {status['cycle_end']}：已用 {status['cycle_calls']}"
        f" / {status['monthly_quota']}，剩 {status['cycle_remaining']}（闸门 {args.keep}）"
    )
    print(f"目标：最近 {len(trade_days)} 个交易日（{start} ~ {end}）→ 切成 {len(segments)} 段")

    if args.codes:
        codes = [c.strip().zfill(6) for c in args.codes.split(",") if c.strip()]
    else:
        codes = _ordered_codes()  # 自选 → 流动性降序（见函数说明）
    if args.limit:
        codes = codes[: args.limit]
    if not codes:
        print("股票池为空：先跑 python scripts/collect_kline.py --pool")
        return 1

    have = _coverage(codes, start, end)
    # 一个交易日一行，缺 10% 以内就不管了（偶尔一两天来源自己没有）
    need_rows = int(len(trade_days) * 0.9)
    pending = [
        code for code in codes if args.refresh or have.get(code, 0) < need_rows
    ]
    print(
        f"池子 {len(codes)} 只：要补 {len(pending)} 只，已是这部历史的 {len(codes) - len(pending)} 只"
        f"（判据：区间内已有 ≥ {need_rows} 行）"
    )
    if args.stats:
        by_count: dict[str, int] = {}
        for code in codes:
            n = have.get(code, 0)
            bucket = "无" if n == 0 else ("<30" if n < 30 else ("30~90" if n < 90 else "≥90"))
            by_count[bucket] = by_count.get(bucket, 0) + 1
        print("  覆盖分布（行数）：", by_count)
        print("  补的先后顺序（前 8 只）：", pending[:8])
        return 0
    if not pending:
        return 0

    started = time.monotonic()
    written = calls = failed = empty = 0
    failures: list[tuple[str, str]] = []
    stopped_at = None
    pool = ThreadPoolExecutor(max_workers=args.workers)
    fill_only = not args.refresh
    try:
        futures = {
            pool.submit(_fetch_one, code, segments, fill_only=fill_only): code
            for code in pending
        }
        for done, future in enumerate(as_completed(futures), start=1):
            code = futures[future]
            try:
                rows, used = future.result()
            except IfindError as exc:  # 源自己的错误（含配额）
                failed += 1
                failures.append((code, str(exc)[:120]))
                if failed >= 20 and failed > done * 0.3:
                    print(f"\n失败 {failed}/{done} 超过三成，先停下（源或配额出问题了）")
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
                continue
            except Exception as exc:  # noqa: BLE001 - 单只票的异常不该带走整批
                failed += 1
                failures.append((code, f"{type(exc).__name__}: {str(exc)[:100]}"))
                continue
            written += rows
            calls += used
            if rows == 0:
                empty += 1
            if done % 25 == 0 or done == len(pending):
                elapsed = time.monotonic() - started
                left = usage.quota_status()["cycle_remaining"]
                logger.info(
                    "  %d/%d 只：写 %d 行，调用 %d 次，空 %d，失败 %d，配额剩 %d，已用 %.0fs",
                    done,
                    len(pending),
                    written,
                    calls,
                    empty,
                    failed,
                    left,
                    elapsed,
                )
                # 配额闸门：留够当天的采集（实测一天 300~500 次）
                if left <= args.keep:
                    stopped_at = f"配额剩 {left} ≤ 闸门 {args.keep}"
                    print(f"\n{stopped_at}，先停在这儿；已补好的不会重抓，下次接着跑")
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
    finally:
        pool.shutdown(wait=False)

    done_count = done if pending else 0
    print(
        f"\n完成：处理 {done_count}/{len(pending)} 只，写入 {written:,} 行，调用 {calls:,} 次，"
        f"无数据 {empty} 只，失败 {failed} 只，用时 {time.monotonic() - started:.0f}s"
    )
    if stopped_at:
        print(f"  提前停止：{stopped_at}（剩 {len(pending) - done_count} 只没跑）")
    for code, message in failures[:5]:
        print(f"  失败样本 {code}: {message}")
    after = usage.quota_status()
    print(f"  配额：已用 {after['cycle_calls']} / {after['monthly_quota']}，剩 {after['cycle_remaining']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
