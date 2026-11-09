"""全市场 RPS（相对强度）扫描：近 20 / 50 / 60 / 120 / 250 日涨幅 → 横截面百分位。

零外部请求、零 iFinD 配额，只读 `stock_daily`。跑一次约 1~3 秒（130 万行流式读），
挂在收盘尾部链上每天重算当天。

## 口径

- 区间涨幅用 `pct_chg` **复利**（库里是不复权价，直接比收盘价会被除权除息带偏）；
- 窗口是**交易日**：两个窗口各自的起点都从 `trade_calendar` 取（第 N 个最近交易日），
  只复利**不早于起点**的行 —— 而不是「最近 N 行」。停牌缺天时 N 行会跨过不止 N 个
  交易日，窗口被悄悄拉长（2026-10-24 修）；
- 窗口内实际行数至少要有 90%，否则该档记 None；
- 分位用**中位秩**（与恐贪、估值分位同一套定义），0 = 全市场最弱、100 = 最强；
  **每个窗口的分母 = 当天该窗口有值的票**（窗口不同、覆盖率不同，分母可以不同；
  这不是 bug，但必须在报告里写明，不能把 20 日分位与 250 日分位当成同一只横截面）；
- **当天必须有这一行的票才参与排名**：最后一根 K 线早于 `target` 的（停牌）不进当天
  横截面，否则它和别人的收益截止在不同日子，排名没有可比性；
- **当天覆盖率闸**：`target` 当天有 `pct_chg` 的行数达不到股票池的 90% 时**不写库**
  （与 `scan_patterns._require_bars` 同一判据）。否则一部分票用当天、一部分用前一天，
  却混在同一张横截面排名里。历史回补时通过 `pool_count` 传入「当时」的估计股票池，
  **不拿当前 `stock_universe` 去卡所有历史日期**。

## 为什么只存「当天一行」

RPS 是横截面指标，历史值对「今天选股」没有增量；存全市场每天一行已经够用
（约 5000 行/天）。将来要做历史回测再按需回补。

## B0 新增（2026-11-08）

- 新增 20 / 60 日两档（`ret20/rps20`、`ret60/rps60`），与旧三档同口径；
- `scan(..., with_meta=True)` 返回 `pool` 与各窗口分母，供回补脚本审计；
- `estimate_pool_count()` 用「目标日之前 20 个交易日的日行数中位数」估计当时的股票池，
  只用于历史覆盖率判断；这是**估计值**，不是真实历史成分，报告里必须标注。
"""

import logging
import time
from datetime import date

from sqlalchemy import func, select

from app.db import session_scope, upsert_many
from app.models import RpsDaily, StockDaily, StockUniverse, TradeCalendar
from app.services.coverage import coverage_ok
from app.services.rps import MIN_COVERAGE, mid_rank, ret, window_values

logger = logging.getLogger(__name__)

#: 五个窗口（交易日）。20/60 是 2026-11-08 B0 新增；50 是 2026-10-24 加的
#: （选股池默认档「双高」要用）；120/250 是原有口径。**新增窗口不改旧窗口含义**。
WINDOWS = (20, 50, 60, 120, 250)

#: 一次流式读多少行。130 万行整表读进内存约几百 MB，按块读只占常数内存。
_STREAM_BATCH = 20000

# 兼容旧调用（测试与回测都按这两个名字引用过）
_ret = ret
_mid_rank = mid_rank
_window_values = window_values


def estimate_pool_count(session, target: date, lookback: int = 20) -> int:
    """估计 `target` 当天的股票池规模（历史回补用）。

    取 `target` **之前** `lookback` 个交易日的「当天有 `pct_chg` 的行数」中位数。
    这是估计值：它能识别「某一天只采了几百只」这种残缺日，但识别不了「整段窗口
    都残缺」；真实历史成分股无法从现有库恢复，报告里必须标注这是估计口径。
    """
    import statistics

    from app.models import TradeCalendar

    days = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date < target)
            .order_by(TradeCalendar.trade_date.desc())
            .limit(lookback)
        )
    )
    counts = [
        int(
            session.scalar(
                select(func.count())
                .select_from(StockDaily)
                .where(
                    StockDaily.trade_date == day,
                    StockDaily.pct_chg.is_not(None),
                )
            )
            or 0
        )
        for day in days
    ]
    counts = [count for count in counts if count > 0]
    return int(statistics.median(counts)) if counts else 0


def scan(
    trade_date: date | None = None,
    *,
    pool_count: int | None = None,
    with_meta: bool = False,
) -> dict:
    """重算 `trade_date`（缺省 = 库里最新日线日）的 RPS，返回写入行数。

    `pool_count`：历史回补时传入「当时股票池」的估计值（见 `estimate_pool_count`）；
    缺省仍用当前 `stock_universe`（与加参数前完全一致）。

    `with_meta=True` 时额外返回 `pool / pool_basis / denominators / day_rows`，
    供回补脚本与审计报告使用；默认不返回，保持旧调用方逐字兼容。
    """
    started = time.monotonic()
    with session_scope() as session:
        target = trade_date or session.scalar(
            select(func.max(StockDaily.trade_date)).where(StockDaily.pct_chg.is_not(None))
        )
        if target is None:
            result = {"rows": 0, "trade_date": None, "cost_seconds": 0.0}
            if with_meta:
                result.update({"pool": 0, "pool_basis": "none", "denominators": {}, "day_rows": 0})
            return result

        # 覆盖率闸：当天没采全就不写库。判据与 `scan_patterns._require_bars` 同一份。
        if pool_count is None:
            pool = session.scalar(select(func.count()).select_from(StockUniverse)) or 0
            pool_basis = "stock_universe"
        else:
            pool = int(pool_count)
            pool_basis = "estimated_history"
        day_rows = (
            session.scalar(
                select(func.count())
                .select_from(StockDaily)
                .where(
                    StockDaily.trade_date == target,
                    StockDaily.pct_chg.is_not(None),
                )
            )
            or 0
        )
        if day_rows == 0 or (pool and not coverage_ok(day_rows, pool)):
            logger.warning(
                "RPS 停跑：%s 的日线只有 %d 行（池子 %d 只，口径 %s），没达到 %.0f%% —— 先补 collect_kline",
                trade_date or target,
                day_rows,
                pool,
                pool_basis,
                MIN_COVERAGE * 100,
            )
            result = {
                "rows": 0,
                "trade_date": target,
                "skipped": "coverage",
                "cost_seconds": round(time.monotonic() - started, 2),
            }
            if with_meta:
                result.update(
                    {
                        "pool": pool,
                        "pool_basis": pool_basis,
                        "denominators": {},
                        "day_rows": day_rows,
                    }
                )
            return result

        trade_days = list(
            session.scalars(
                select(TradeCalendar.trade_date)
                .where(TradeCalendar.trade_date <= target)
                .order_by(TradeCalendar.trade_date.desc())
                .limit(max(WINDOWS))
            )
        )
        if not trade_days:
            result = {"rows": 0, "trade_date": target, "cost_seconds": 0.0}
            if with_meta:
                result.update(
                    {
                        "pool": pool,
                        "pool_basis": pool_basis,
                        "denominators": {},
                        "day_rows": day_rows,
                    }
                )
            return result
        # 各窗口的起点：第 N 个最近交易日（不够 N 天时用最早那天）
        starts = {
            window: trade_days[min(window, len(trade_days)) - 1] for window in WINDOWS
        }
        start = min(starts.values())

        statement = (
            select(StockDaily.code, StockDaily.trade_date, StockDaily.pct_chg)
            .where(
                StockDaily.trade_date >= start,
                StockDaily.trade_date <= target,
                StockDaily.pct_chg.is_not(None),
            )
            .order_by(StockDaily.code, StockDaily.trade_date)
            .execution_options(yield_per=_STREAM_BATCH)
        )

        returns: dict[str, dict[int, float | None]] = {}
        current = ""
        rows: list[tuple[date, float]] = []

        def flush() -> None:
            if not current:
                return
            # 当天没有可用日线（停牌 / 漏采）→ 不参与当天横截面排名
            if not rows or rows[-1][0] != target:
                return
            computed = {
                window: _ret(_window_values(rows, starts[window]), window)
                for window in WINDOWS
            }
            if any(value is not None for value in computed.values()):
                returns[current] = computed

        for code, day, pct in session.execute(statement):
            if code != current:
                flush()
                current = code
                rows = []
            rows.append((day, float(pct)))
        flush()

        # 分位：先按各窗口筛出有值的票，再排序做中位秩
        ordered = {
            window: sorted(
                value[window] for value in returns.values() if value[window] is not None
            )
            for window in WINDOWS
        }
        if not any(ordered.values()):
            result = {"rows": 0, "trade_date": target, "cost_seconds": 0.0}
            if with_meta:
                result.update(
                    {
                        "pool": pool,
                        "pool_basis": pool_basis,
                        "denominators": {window: 0 for window in WINDOWS},
                        "day_rows": day_rows,
                    }
                )
            return result

        records = []
        for code, computed in returns.items():
            record: dict = {"trade_date": target, "code": code}
            for window, value in computed.items():
                record[f"ret{window}"] = value
                record[f"rps{window}"] = (
                    None if value is None else _mid_rank(ordered[window], value)
                )
            records.append(record)

        written = upsert_many(session, RpsDaily, records)

    cost = time.monotonic() - started
    logger.info(
        "RPS 扫描 %s：%d 只（%s），用时 %.1fs",
        target,
        written,
        " / ".join(f"{window} 日 {len(ordered[window])} 只" for window in WINDOWS),
        cost,
    )
    result = {"rows": written, "trade_date": target, "cost_seconds": round(cost, 2)}
    if with_meta:
        result.update(
            {
                "pool": pool,
                "pool_basis": pool_basis,
                "denominators": {window: len(ordered[window]) for window in WINDOWS},
                "day_rows": day_rows,
            }
        )
    return result

