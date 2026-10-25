"""板块强度 → 个股：IC、强势板块选股、强势板块 × 突破形态三种检验。

## 前提

`sector_member` 必须是**逐日、全板块**的历史成分 —— 用
`scripts/backfill_sector_members.py` 回补。拿今天的成分回看历史会引入前视偏差
（当时不在板块里的票会被算进来）。

## 口径（与既有回测完全一致）

- 板块强度 = `sector_rps.rps20_on`：板块指数 20 日复利涨幅的横截面分位（页面同口径）；
- 买入 = D+1 **开盘价**，卖出 = D+1 **收盘价**；D+1 开盘已在涨停板的剔除
  （`backtest_relay._tradable`，与首板/接力回测同一套判据）；
- 基准 = 当日全市场等权（同一套剔除）。

## 三种检验

1. `ic`：每日横截面 Spearman IC —— 「个股所属最强板块的 RPS20」对「该股 D+1 收益」
   有没有预测力；顺带看五分位分组的收益单调性；
2. `pick`：RPS20 ≥ 90 的前 N 个板块，每个板块取涨幅前 3 / 量价因子分前 3，
   与「弱势板块（RPS20 ≤ 10）同样取法」对照；
3. `breakout`：强势板块成分股里当天命中突破形态的票，与「全市场当天命中突破」对照。

    python scripts/backtest_sector_stock.py                  # 三种都跑
    python scripts/backtest_sector_stock.py --mode ic
    python scripts/backtest_sector_stock.py --days 60 --sectors 3 --per-sector 3
"""

import argparse
import logging
import math
import statistics
import sys
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
BACKEND = SCRIPTS.parent / "backend"
for path in (str(BACKEND), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import numpy as np  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.models import PatternHit, SectorBasic, SectorMember, StockDaily, TradeCalendar  # noqa: E402
from app.services import factor_score, sector_rps  # noqa: E402
from app.services.pools import BREAKOUT_KEYS  # noqa: E402
from backtest_relay import _load_window, _tradable  # noqa: E402
from factor_ic import _spearman  # noqa: E402  （同一套秩相关，不另写一份）

logger = logging.getLogger("sector_stock")

#: 强势 / 弱势板块的分位线
STRONG = 90.0
WEAK = 10.0

#: IC 的五分位分组（左闭右开，最后一档含 100）
BUCKETS = 5


def _pairs(days: int) -> list[tuple[date, date]]:
    """最近的 `days` 个「有成分股 + 次日有行情」的 (信号日, 次日)。"""
    with session_scope() as session:
        calendar = list(
            session.scalars(select(TradeCalendar.trade_date).order_by(TradeCalendar.trade_date))
        )
        position = {day: index for index, day in enumerate(calendar)}
        data_end = session.scalar(select(func.max(StockDaily.trade_date)))
        member_days = sorted(
            session.scalars(select(SectorMember.trade_date).group_by(SectorMember.trade_date))
        )
    out = [
        (day, calendar[position[day] + 1])
        for day in member_days
        if position.get(day) is not None
        and position[day] + 1 < len(calendar)
        and data_end is not None
        and calendar[position[day] + 1] <= data_end
    ]
    return out[-days:] if days else out


def _members(session, day: date, taxonomy: str) -> list[tuple[str, str, float | None]]:
    """当天该口径下全部 (个股, 板块, 当日涨跌幅)。"""
    return session.execute(
        select(SectorMember.code, SectorMember.sector_code, SectorMember.pct_chg)
        .join(SectorBasic, SectorBasic.code == SectorMember.sector_code)
        .where(SectorMember.trade_date == day, SectorBasic.taxonomy == taxonomy)
    ).all()


def _stock_rps(members: list[tuple[str, str, float | None]], rps20: dict[str, float]) -> dict[str, float]:
    """个股 → 它所属**最强**板块的 RPS20（一只票挂多个板块时取最大值）。"""
    out: dict[str, float] = {}
    for code, sector, _pct in members:
        value = rps20.get(sector)
        if value is None:
            continue
        if value > out.get(code, -1.0):
            out[code] = value
    return out


def _market(quotes: dict[str, tuple]) -> float:
    values = [
        close / open_ - 1
        for code, (open_, close, pct) in quotes.items()
        if open_ and close and _tradable(code, open_, close, pct)
    ]
    return sum(values) / len(values) if values else float("nan")


def _stats(returns: list[float], excesses: list[float]) -> tuple[float, float, float, float]:
    if not returns:
        return float("nan"), float("nan"), float("nan"), float("nan")
    mean = sum(returns) / len(returns)
    median = statistics.median(returns)
    win = sum(1 for value in returns if value > 0) / len(returns)
    excess = sum(excesses) / len(excesses) if excesses else float("nan")
    return mean, median, win, excess


def _fmt_row(label: str, returns: list[float], excesses: list[float], missing: int) -> str:
    mean, median, win, excess = _stats(returns, excesses)
    return (
        f"{label:<18}{len(returns):>7}{mean * 100:>8.2f}%{median * 100:>8.2f}%"
        f"{win * 100:>7.1f}%{excess * 100:>9.2f}%{missing:>8}"
    )


def run_ic(pairs, taxonomy: str, data) -> None:
    ics: list[float] = []
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(BUCKETS)]
    with session_scope() as session:
        for day, following in pairs:
            rps20 = sector_rps.rps20_on(session, day, taxonomy)
            if not rps20:
                continue
            members = _members(session, day, taxonomy)
            stock_rps = _stock_rps(members, rps20)
            quotes = data.get(following, {})
            market = _market(quotes)

            xs: list[float] = []
            ys: list[float] = []
            for code, value in stock_rps.items():
                quote = quotes.get(code)
                if quote is None:
                    continue
                open_, close, pct = quote
                if not open_ or not close or not _tradable(code, open_, close, pct):
                    continue
                ret = close / open_ - 1
                xs.append(value)
                ys.append(ret)
                index = min(int(value // (100 / BUCKETS)), BUCKETS - 1)
                buckets[index].append((ret, market))

            if len(xs) < 50:
                continue
            ic = _spearman(np.asarray(xs, dtype=float), np.asarray(ys, dtype=float))
            if ic == ic:
                ics.append(ic)

    print()
    if not ics:
        print("IC：没有可用样本（先确认 sector_member 有历史成分）")
        return
    mean = sum(ics) / len(ics)
    std = statistics.pstdev(ics) if len(ics) > 1 else float("nan")
    ir = mean / std if std and std == std else float("nan")
    t_value = ir * math.sqrt(len(ics)) if ir == ir else float("nan")
    positive = sum(1 for value in ics if value > 0) / len(ics)
    print(f"IC（板块 RPS20 → 个股 D+1 收益）：{len(ics)} 天")
    print(
        f"  平均 IC {mean:+.4f}  IR {ir:+.2f}  t {t_value:+.1f}  正 IC 天数 {positive * 100:.0f}%"
    )
    print()
    print(f"{'板块 RPS 分组':<16}{'样本':>8}{'均值':>9}{'中位':>9}{'胜率':>8}{'超额':>9}")
    print("-" * 60)
    for index, group in enumerate(buckets):
        low, high = index * (100 // BUCKETS), (index + 1) * (100 // BUCKETS)
        if not group:
            continue
        returns = [value for value, _ in group]
        excesses = [value - market for value, market in group if market == market]
        mean, median, win, excess = _stats(returns, excesses)
        print(
            f"{f'{low}~{high}':<16}{len(returns):>8}{mean * 100:>8.2f}%"
            f"{median * 100:>8.2f}%{win * 100:>7.1f}%{excess * 100:>8.2f}%"
        )


def _top_sectors(rps20: dict[str, float], count: int, *, strong: bool) -> list[str]:
    ranked = sorted(rps20.items(), key=lambda item: item[1], reverse=strong)
    if strong:
        return [code for code, value in ranked if value >= STRONG][:count]
    return [code for code, value in ranked if value <= WEAK][:count]


def run_pick(pairs, taxonomy: str, data, sectors: int, per_sector: int) -> None:
    buckets = {
        label: {"ret": [], "excess": [], "missing": 0}
        for label in ("强势·涨幅", "强势·因子分", "弱势·涨幅")
    }
    with session_scope() as session:
        for day, following in pairs:
            rps20 = sector_rps.rps20_on(session, day, taxonomy)
            if not rps20:
                continue
            members = _members(session, day, taxonomy)
            quotes = data.get(following, {})
            market = _market(quotes)

            by_sector: dict[str, list[tuple[str, float]]] = {}
            for code, sector, pct in members:
                if pct is None:
                    continue
                by_sector.setdefault(sector, []).append((code, float(pct)))

            def collect(sector_codes: list[str], use_factor: bool) -> list[str]:
                picked: list[str] = []
                seen: set[str] = set()
                pool = [code for sector in sector_codes for code, _ in by_sector.get(sector, [])]
                scored = (
                    factor_score.scores(session, pool, day) if use_factor else {}
                )
                for sector in sector_codes:
                    rows = by_sector.get(sector, [])
                    if use_factor:
                        rows = sorted(
                            rows,
                            key=lambda item: -(
                                scored[item[0]].score if item[0] in scored else -1.0
                            ),
                        )
                    else:
                        rows = sorted(rows, key=lambda item: -item[1])
                    for code, _pct in rows[:per_sector]:
                        if code not in seen:
                            seen.add(code)
                            picked.append(code)
                return picked

            strong = _top_sectors(rps20, sectors, strong=True)
            weak = _top_sectors(rps20, sectors, strong=False)
            plans = [
                ("强势·涨幅", collect(strong, use_factor=False)),
                ("强势·因子分", collect(strong, use_factor=True)),
                ("弱势·涨幅", collect(weak, use_factor=False)),
            ]
            for label, codes in plans:
                bucket = buckets[label]
                for code in codes:
                    quote = quotes.get(code)
                    if quote is None:
                        bucket["missing"] += 1
                        continue
                    open_, close, pct = quote
                    if not _tradable(code, open_, close, pct):
                        bucket["missing"] += 1
                        continue
                    ret = close / open_ - 1
                    bucket["ret"].append(ret)
                    if market == market:
                        bucket["excess"].append(ret - market)

    print()
    print(f"强势板块选股（RPS20 ≥ {STRONG:.0f} 前 {sectors} 个板块 × 每板块前 {per_sector} 只）")
    print()
    print(f"{'口径':<18}{'样本':>7}{'均值':>9}{'中位':>9}{'胜率':>8}{'超额':>9}{'买不到':>8}")
    print("-" * 68)
    for label, bucket in buckets.items():
        print(_fmt_row(label, bucket["ret"], bucket["excess"], bucket["missing"]))


def run_breakout(pairs, taxonomy: str, data, sectors: int) -> None:
    buckets = {
        "强势板块 × 突破": {"ret": [], "excess": [], "missing": 0},
        "全市场 × 突破": {"ret": [], "excess": [], "missing": 0},
    }
    with session_scope() as session:
        for day, following in pairs:
            rps20 = sector_rps.rps20_on(session, day, taxonomy)
            if not rps20:
                continue
            members = _members(session, day, taxonomy)
            quotes = data.get(following, {})
            market = _market(quotes)

            strong = set(_top_sectors(rps20, sectors, strong=True))
            in_strong = {code for code, sector, _pct in members if sector in strong}
            hits = set(
                session.scalars(
                    select(PatternHit.code).where(
                        PatternHit.trade_date == day,
                        PatternHit.pattern.in_(BREAKOUT_KEYS),
                    )
                )
            )
            if not hits:
                continue

            plans = [
                ("强势板块 × 突破", sorted(in_strong & hits)),
                ("全市场 × 突破", sorted(hits)),
            ]
            for label, codes in plans:
                bucket = buckets[label]
                for code in codes:
                    quote = quotes.get(code)
                    if quote is None:
                        bucket["missing"] += 1
                        continue
                    open_, close, pct = quote
                    if not _tradable(code, open_, close, pct):
                        bucket["missing"] += 1
                        continue
                    ret = close / open_ - 1
                    bucket["ret"].append(ret)
                    if market == market:
                        bucket["excess"].append(ret - market)

    print()
    print(f"强势板块（RPS20 ≥ {STRONG:.0f} 前 {sectors} 个）× 当日突破形态（{len(BREAKOUT_KEYS)} 种）")
    print()
    print(f"{'口径':<18}{'样本':>7}{'均值':>9}{'中位':>9}{'胜率':>8}{'超额':>9}{'买不到':>8}")
    print("-" * 68)
    for label, bucket in buckets.items():
        print(_fmt_row(label, bucket["ret"], bucket["excess"], bucket["missing"]))


def main() -> int:
    parser = argparse.ArgumentParser(description="板块强度 → 个股的三种检验")
    parser.add_argument("--mode", choices=("ic", "pick", "breakout", "all"), default="all")
    parser.add_argument("--days", type=int, default=60, help="最近多少个信号日")
    parser.add_argument("--taxonomy", default="kph_selected", choices=("kph_selected", "kph_industry"))
    parser.add_argument("--sectors", type=int, default=3, help="取几个强势板块")
    parser.add_argument("--per-sector", type=int, default=3, help="每个板块取几只")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    pairs = _pairs(args.days)
    if not pairs:
        raise SystemExit("没有可用信号日：先跑 scripts/backfill_sector_members.py 回补成分股")

    days = sorted({day for pair in pairs for day in pair})
    with session_scope() as session:
        data = _load_window(session, days)

    print(
        f"信号日 {len(pairs)} 天（{pairs[0][0]} ~ {pairs[-1][0]}）；"
        f"口径 {args.taxonomy}；D+1 开盘买、D+1 收盘卖"
    )
    if args.mode in ("ic", "all"):
        run_ic(pairs, args.taxonomy, data)
    if args.mode in ("pick", "all"):
        run_pick(pairs, args.taxonomy, data, args.sectors, args.per_sector)
    if args.mode in ("breakout", "all"):
        run_breakout(pairs, args.taxonomy, data, args.sectors)
    print()
    print("⚠️ 未扣费用；样本只有几十个交易日时，别拿一两档的差异定策略")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
