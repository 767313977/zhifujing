"""把日线历史往回补几年（本地回测用），写进独立的 `history.db`。

## 为什么单独一个库，不直接写 stock_daily

采集任务有 `KlineCollector.prune()`：只留最近 `kline_keep_days`(400) 个交易日，理由写在
它的 docstring 里 ——「形态最长看 250 日」+「重采历史很贵（一天 13 次 iFinD 调用）……
剪掉的历史实际上找不回来」。所以把几年的历史写进 `stock_daily` 是**留不住**的：
下一次采集（或启动补采）就删了，而且删了之后按原口径找不回来。

回测要的是几年前的数据，与线上扫描只需 260 天是两件事，所以分开存：

| 用途 | 库 | 口径 | 保留 |
| --- | --- | --- | --- |
| 站点 / 线上扫描 | `stock_daily` | iFinD 不复权 + 真实涨跌幅 | 最近 400 个交易日 |
| 本地回测 | `backend/data/history.db` | 腾讯不复权（同一口径） | 补多少留多少 |

两边**同一张表名、同一套列**，所以回测脚本只要换个引擎就能复用同一段查询
（见 `backtest_patterns.py --history`）。

## 源的选择：默认腾讯，不用东财

**东财当前从本机连不上**（2026-09-25 实测）：`push2his.eastmoney.com` 的 TCP 443 通，
但 HTTP 层被重置 —— `curl` 直接返回 `http=000`（0.33s，像是被 RST），
换 UA、换 `lmt`/`beg` 各种参数组合都一样。同一时刻腾讯 `web.ifzq.gtimg.cn` 返回 200。
所以补历史走腾讯（`--source eastmoney` 留着，等哪天东财通了可以换回去对账）。

腾讯这条线一次调用能取到 **3 年 / 743 行**、10 年 / 2444 行（实测 2026-09-25），
**不封顶**，所以窗口取多长代价一样 —— 默认按 5 年补。

**吞吐（实测，决定你得等多久）**：每只票要拉两份（不复权 + 前复权，涨跌幅由后者推），
约 6 秒/只；**4 个线程是最快的**（≈3.3 秒/只），10 个线程反而掉到 0.22 只/秒
—— 腾讯对高并发会限速，threads 开大不是加速是排队。全池 3000 只按 4 线程要两个半小时，
所以分批跑或抽样跑（`--stride`）更现实；脚本可中断重跑，已补够的会跳过。

**其它源当前都不可用**（2026-09-25 逐个实测）：
- 东财 `push2his.eastmoney.com`：TCP 443 通，但 HTTP 层被重置（curl 返回 `http=000`，
  0.33s，像被 RST），换 UA / 换 `lmt` / 换 `beg` 各种组合都一样
- 网易 `quotes.money.163.com/service/chddata.html`：三个测试代码全部返回 **502**
  （本来它最理想：一次请求给全历史 CSV，且直接带官方涨跌幅与换手率）
- 悟道之路的本地 `data/market.db`：这台机器上没有这个文件

## 用法

    python scripts/backfill_history.py --stats               # 只看现状，不发请求
    python scripts/backfill_history.py --years 5             # 全池补 5 年（很久，见上面吞吐）
    python scripts/backfill_history.py --years 5 --stride 6   # 跨全市场每 6 只取 1 只（约 500 只）
    python scripts/backfill_history.py --years 5 --limit 20   # 只前 20 只（冒烟测试用）
    python scripts/backfill_history.py --codes 600000,000001 --years 5
    python scripts/backfill_history.py --years 5 --refresh    # 已有的也重抓（默认跳过）
    python scripts/backfill_history.py --source eastmoney     # 东财通了之后换源对账
    python scripts/backfill_history.py --workers 2            # 想再保守一点

可反复跑、可中断重跑：同一只票已经有足够年头的历史时**默认跳过**（省请求），
`--refresh` 强制重抓（口径升级时用）。零 iFinD 配额。
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

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import init_db, session_scope, upsert_many  # noqa: E402
from app.models import StockBasic, StockDaily  # noqa: E402
from app.sources.eastmoney import fetch_daily as fetch_daily_eastmoney  # noqa: E402
from app.sources.tencent import fetch_daily as fetch_daily_tencent  # noqa: E402

logger = logging.getLogger("backfill_history")

HISTORY_DB = BACKEND / "data" / "history.db"
DAYS_PER_YEAR = 365.25
TRADING_DAYS_PER_YEAR = 240  # 只用来判断「补够了没有」，不必精确

# 前复权序列的第一行没有前值、算不出涨跌幅会被丢掉，多取两周兜住
_LEAD_DAYS = 14

_DEFAULT_WORKERS = 8


def _engine():
    engine = create_engine(f"sqlite:///{HISTORY_DB.as_posix()}")
    # 只建这一张表：这是回测库，别的表用不上（create_all 会建出二十张空表）
    StockDaily.__table__.create(engine, checkfirst=True)
    return engine


def _spans(engine) -> dict[str, tuple[date, int]]:
    """每只票在 history.db 里已有的 `(最早日期, 行数)`。"""
    with Session(engine) as session:
        rows = session.execute(
            select(StockDaily.code, func.min(StockDaily.trade_date), func.count()).group_by(
                StockDaily.code
            )
        ).all()
    return {code: (low, count) for code, low, count in rows if low is not None}


def _stats(engine) -> None:
    spans = _spans(engine)
    size = HISTORY_DB.stat().st_size / 1e6 if HISTORY_DB.is_file() else 0.0
    print(f"库：{HISTORY_DB}（{size:.1f} MB）")
    if not spans:
        print("还是空的 —— 跑一次不带 --stats 的命令开始补")
        return
    counts = sorted(count for _, count in spans.values())
    lows = sorted(low for low, _ in spans.values())
    print(f"票数 {len(spans)}，总行数 {sum(counts):,}")
    print(f"最早日期：{lows[0]} ~ {lows[-1]}（中位 {lows[len(lows) // 2]}）")
    print(
        f"每票行数：min {counts[0]}  p25 {counts[len(counts) // 4]}  "
        f"中位 {counts[len(counts) // 2]}  p75 {counts[len(counts) * 3 // 4]}  max {counts[-1]}"
    )


def _names() -> dict[str, str]:
    """主库里的代码 → 名称。批量补时一次取完，省掉每只票一次查询。"""
    with session_scope() as session:
        rows = session.execute(select(StockBasic.code, StockBasic.name)).all()
    return {code: (name or code) for code, name in rows}


def _fetch(code: str, *, source: str, start: date, end: date, years: float) -> list[dict]:
    if source == "eastmoney":
        # 东财接口按「最近多少个日历日」取，不给区间
        return fetch_daily_eastmoney(code, days=int(DAYS_PER_YEAR * years) + _LEAD_DAYS)
    return fetch_daily_tencent(code, start=start, end=end)


def main() -> int:
    parser = argparse.ArgumentParser(description="把日线历史补进 history.db（零 iFinD 配额）")
    parser.add_argument("--years", type=float, default=3.0, help="往回补几年（默认 3）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 只（0 = 全部）")
    parser.add_argument(
        "--stride",
        type=int,
        default=1,
        help="抽样步长：每 N 只取一只。**想覆盖全市场就该用它而不是 --limit** —— "
        "池子是按代码排的，取前 N 只等于只覆盖深市小代码那一段（实测头 400 只全是 000/001/002）",
    )
    parser.add_argument("--codes", default="", help="只处理这些代码，逗号分隔")
    parser.add_argument("--refresh", action="store_true", help="已经够年头也重抓一遍")
    parser.add_argument("--stats", action="store_true", help="只看现状，不发请求")
    parser.add_argument(
        "--source", choices=("tencent", "eastmoney"), default="tencent", help="取数源（默认腾讯）"
    )
    parser.add_argument("--workers", type=int, default=_DEFAULT_WORKERS, help="并发线程数")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db()  # 让主库就位（本脚本只从主库读股票池与名字，不写它）
    engine = _engine()

    if args.stats:
        _stats(engine)
        return 0

    if args.codes:
        codes = [c.strip().zfill(6) for c in args.codes.split(",") if c.strip()]
    else:
        from app.jobs.collect_universe import load_codes

        codes = load_codes()
    if args.limit:
        codes = codes[: args.limit]
    if args.stride > 1:
        codes = codes[:: args.stride]
    if not codes:
        print("股票池为空：先跑 python scripts/collect_kline.py --pool")
        return 1

    end = date.today()
    start = end - timedelta(days=int(DAYS_PER_YEAR * args.years) + _LEAD_DAYS)
    min_rows = int(TRADING_DAYS_PER_YEAR * args.years * 0.8)
    existing = _spans(engine)
    names = _names()

    pending = [
        code
        for code in codes
        if args.refresh
        or code not in existing
        or existing[code][0] > start
        or existing[code][1] < min_rows
    ]
    print(
        f"源：{args.source}　目标：{len(pending)} 只补到 {start} 之后"
        f"（{args.years:g} 年，约 {min_rows}+ 根）"
    )
    print(f"跳过 {len(codes) - len(pending)} 只（已是这部历史）　并发 {args.workers}\n")
    if not pending:
        _stats(engine)
        return 0

    started = time.monotonic()
    written = failed = empty = 0
    failures: list[tuple[str, str]] = []
    pool = ThreadPoolExecutor(max_workers=args.workers)
    try:
        futures = {
            pool.submit(_fetch, code, source=args.source, start=start, end=end, years=args.years): code
            for code in pending
        }
        for done, future in enumerate(as_completed(futures), start=1):
            code = futures[future]
            try:
                rows = future.result()
            except Exception as exc:  # noqa: BLE001
                failed += 1
                failures.append((code, str(exc)))
                # 熔断：失败比例过高说明是源的问题，继续跑只是白等（也免得把源打急）
                if failed >= 30 and failed > done * 0.2:
                    print(f"\n失败 {failed}/{done} 超过两成，先停下（源多半出了问题）")
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
                continue
            if not rows:
                empty += 1  # 退市 / 未上市，属正常
                continue
            name = names.get(code, code)
            for row in rows:
                row["name"] = name
            with Session(engine) as session:
                written += upsert_many(session, StockDaily, rows)
                session.commit()
            if done % 100 == 0 or done == len(pending):
                elapsed = time.monotonic() - started
                logger.info(
                    "  %d/%d 只：写 %d 行，空 %d，失败 %d，已用 %.0fs（剩约 %.0fs）",
                    done,
                    len(pending),
                    written,
                    empty,
                    failed,
                    elapsed,
                    elapsed / done * (len(pending) - done),
                )
    finally:
        pool.shutdown(wait=False)

    print(
        f"\n完成：写入/更新 {written:,} 行，无数据 {empty} 只，失败 {failed} 只，"
        f"用时 {time.monotonic() - started:.0f}s"
    )
    for code, message in failures[:5]:
        print(f"  失败样本 {code}: {message[:100]}")
    print()
    _stats(engine)
    return 0


if __name__ == "__main__":
    sys.exit(main())
