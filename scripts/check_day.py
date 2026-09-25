"""核对某一天的采集与命中：链路、日线覆盖率、命中价对账、iFinD 用量。

用法:
    python scripts/check_day.py                 # 最近一个有数据的交易日
    python scripts/check_day.py 2026-09-28

每一项都是「那天到底对不对」的直接判据，改动采集/扫描口径之后跑一遍即可。
2026-09-24 那次「命中的最新价是旧价」（设计文档 8.64）就是第 2、3 项先出的红灯：
池内缺了 274 只没被发现，命中里 44 条带着隔夜的价。

退出码 0 = 全部正常，1 = 有红灯（便于挂成定时任务后看结果）。

⚠️ 在**本机**跑必然有一项红灯：`push_brief 没有记录` —— 本机是当看图机用的
（`SCHEDULER_ENABLED=false`），采集与简报都归服务器。核对生产数据请到云端跑。

在云端跑（服务器的包在 /opt/fupan）:
    PYTHONPATH=/opt/fupan/backend /opt/fupan/.venv/bin/python /opt/fupan/scripts/check_day.py
"""

import os
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, os.environ.get("FUPAN_BACKEND", str(Path(__file__).resolve().parents[1] / "backend")))

from sqlalchemy import func, select

from app.db import session_scope
from app.models import (
    CollectLog,
    IfindUsage,
    PatternHit,
    StockDaily,
    StockUniverse,
    TradeCalendar,
)

# 覆盖率低于这个就报红灯。与采集端 `_RECENT_MISSING_RATIO` 同一个口径：
# 正常缺失（停牌 / 次新）约 0.3%，超过 1% 就是「没采全」
MIN_COVERAGE = 0.99


def _arg_date() -> date:
    """命令行给了就用它，否则取库里最近一天的命中（没有命中就取最近日线）。"""
    if len(sys.argv) > 1:
        return date.fromisoformat(sys.argv[1])
    with session_scope() as session:
        found = session.scalar(select(func.max(PatternHit.trade_date)))
        if found is None:
            found = session.scalar(select(func.max(StockDaily.trade_date)))
    if found is None:
        raise SystemExit("库里还没有日线，先跑 collect_kline")
    return found


def main() -> int:
    target = _arg_date()
    problems: list[str] = []
    print(f"核对 {target}    （服务器时间 {datetime.now():%Y-%m-%d %H:%M:%S}）")

    with session_scope() as session:
        print("\n[0] 交易日确认")
        days = session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date >= target)
            .order_by(TradeCalendar.trade_date)
            .limit(6)
        ).all()
        is_trade_day = session.scalar(
            select(func.count()).where(TradeCalendar.trade_date == target)
        )
        print(f"    {target} 是交易日吗：{bool(is_trade_day)}；"
              f"其后几天：{' '.join(str(d) for d in days)}")
        if not is_trade_day:
            print("    —— 休市日，后面几项都该是空的")
            return 0

        print("\n[1] 采集链路（按发生顺序）")
        logs = session.scalars(
            select(CollectLog).where(CollectLog.trade_date == target).order_by(CollectLog.id)
        ).all()
        if not logs:
            problems.append("那天没有任何采集日志 —— 链路根本没跑")
        for r in logs:
            ts = r.created_at.strftime("%H:%M:%S") if r.created_at else "-"
            mark = "" if r.status == "ok" else f"  ← {r.status}"
            print(f"    {ts} {r.task:<16} {r.status:<8} rows={r.rows!s:<7} {(r.message or '')[:74]}{mark}")
        if not any(r.task == "patterns" and r.status == "ok" for r in logs):
            problems.append("没有成功的形态扫描记录")
        if not any(r.task == "push_brief" for r in logs):
            problems.append("简报没推（push_brief 没有记录）")

        print("\n[2] 日线覆盖率")
        pool = set(session.scalars(select(StockUniverse.code)))
        got = set(
            session.scalars(
                select(StockDaily.code).where(
                    StockDaily.trade_date == target, StockDaily.pct_chg.is_not(None)
                )
            )
        )
        missing = pool - got
        coverage = (len(pool) - len(missing)) / len(pool) if pool else 0
        print(f"    池 {len(pool)}，可用 {len(pool) - len(missing)}（{coverage:.1%}），缺 {len(missing)} 只")
        if missing:
            print(f"    缺的按前缀：{dict(Counter(c[:3] for c in missing).most_common(6))}")
        if coverage < MIN_COVERAGE:
            problems.append(
                f"覆盖率 {coverage:.1%} < {MIN_COVERAGE:.0%}：那天没采全"
                "（看缺的票是否成堆出现在某个前缀段 → iFinD 段内抽风）"
            )

        print("\n[3] 命中价对账（命中的 close 必须等于当天日线）")
        joined = session.execute(
            select(PatternHit.code, PatternHit.close, PatternHit.pct_chg, StockDaily.close, StockDaily.pct_chg)
            .join(
                StockDaily,
                (StockDaily.code == PatternHit.code)
                & (StockDaily.trade_date == PatternHit.trade_date)
                & StockDaily.pct_chg.is_not(None),
            )
            .where(PatternHit.trade_date == target)
        ).all()
        total_hits = (
            session.scalar(
                select(func.count()).select_from(PatternHit).where(PatternHit.trade_date == target)
            )
            or 0
        )
        mismatch = [
            (code, hc, dc) for code, hc, _, dc, _ in joined if hc is not None and abs(hc - dc) > 1e-6
        ]
        print(f"    命中 {total_hits} 条；能对上日线 {len(joined)} 条；价格不一致 **{len(mismatch)}** 条")
        if mismatch:
            print(f"    前 3 条不一致：{mismatch[:3]}")
            problems.append(f"{len(mismatch)} 条命中的价格与当天日线不一致（旧价混进来了）")
        no_daily = total_hits - len(joined)
        if no_daily:
            print(f"    没有当天日线可对账的命中：{no_daily} 条")
            problems.append(f"{no_daily} 条命中的票当天没有日线（不该出信号）")

        print("\n[4] 前 5 名命中")
        for r in session.scalars(
            select(PatternHit)
            .where(PatternHit.trade_date == target)
            .order_by(PatternHit.score.desc())
            .limit(5)
        ):
            print(f"    {r.code} {r.name} 分{r.score} 收{r.close} 涨跌{r.pct_chg:.2f} {r.pattern}")

        print("\n[5] iFinD 用量")
        rows = session.execute(
            select(IfindUsage.tool, func.sum(IfindUsage.calls))
            .where(IfindUsage.usage_date == target)
            .group_by(IfindUsage.tool)
            .order_by(func.sum(IfindUsage.calls).desc())
        ).all()
        print(f"    {sum(n for _, n in rows)} 次：" + "、".join(f"{t} {n}" for t, n in rows))

    print()
    if problems:
        print("红灯：")
        for item in problems:
            print(f"  ✗ {item}")
        return 1
    print("全部正常 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
