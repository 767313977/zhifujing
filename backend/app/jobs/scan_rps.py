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

## 写入与过期语义（2026-11-10）

- 批量写入走 `upsert_many_fill`：**新值为 None 时不覆盖库里的非空值**。行情偶发漏字段
  时不会把已算好的分位刷空。
- 但 COALESCE 只适合「这次没算这一格」，不能用来表达「这一格已经失效」。因此本次重算
  判定为**不可算**的单元格，在**同一事务、同一交易日**内先显式清成 NULL，再 upsert：
  - 当天有 K 线、但该窗口覆盖 <90%（`ret` 返回 None）→ 只清该窗口的 `ret*/rps*`；
  - 当天没有 K 线（停牌 / 行情被修订删掉）→ 该行全部指标清空，**保留主键行**，
    状态语义是「有行、无值 = 缺失 / 待重算」，不是「当前有效值」。
- 清理范围严格限定 `trade_date == target`：历史日期、其他列、其他业务表一律不动；
  与写入同一事务，失败一起回滚。
- **清理前的三道安全前提（2026-11-10 补）**，任一不满足就整轮停跑（不写不清）：
  1. 当天有 K 线（`day_rows > 0`）且达到覆盖率闸 —— 与写入共用同一判据；
  2. 当天行数不低于「前 5 个交易日中位数」的 90%，且绝对缺口 ≥ 100 行 ——
     识别「采集没跑完 / 分批导入跑到一半」，避免把暂时缺行当成历史数据被删；
  3. 待清理行数不超过 `max(50, 库内该日行数 × 5%)` —— 超过即视为行情完整性
     不可信，拒绝破坏性清理并报「需重新采集」。
- 清理 UPDATE 带 `OR(列 IS NOT NULL)`（覆盖每一列）：已经全为 NULL 的行不再产生
  写入，避免无意义地推高 `rps` 域 revision。日志里的计数是**行数**（`rowcount`），
  不是单元格数，两者不得混用。
"""

import logging
import time
from datetime import date

from sqlalchemy import func, or_, select, update

from app.db import session_scope, upsert_many_fill
from app.models import RpsDaily, StockDaily, StockUniverse, TradeCalendar
from app.services.coverage import coverage_ok
from app.services.rps import MIN_COVERAGE, mid_rank, ret, window_values

logger = logging.getLogger(__name__)

#: 五个窗口（交易日）。20/60 是 2026-11-08 B0 新增；50 是 2026-10-24 加的
#: （选股池默认档「双高」要用）；120/250 是原有口径。**新增窗口不改旧窗口含义**。
WINDOWS = (20, 50, 60, 120, 250)

#: 一次流式读多少行。130 万行整表读进内存约几百 MB，按块读只占常数内存。
_STREAM_BATCH = 20000

#: 清理过期单元格时一条 UPDATE 带多少个 code（SQLite 参数上限 32766，留足余量）。
_CLEAR_CHUNK = 500

#: 一行 RPS 的全部指标列（过期时整行清空用）。
_METRIC_COLUMNS = tuple(
    f"{prefix}{window}" for window in WINDOWS for prefix in ("ret", "rps")
)

#: 目标日 K 线行数相对「前 N 个交易日中位数」的容忍下限。低于它视为**采集尚未完成**
#: （停止写入、也停止清理），不拿残缺行情去推断「历史数据被删了」。
_BAR_COMPLETENESS = 0.90
_BAR_BASELINE_DAYS = 5
#: 同时要求绝对缺口达到这么多行，避免小样本（测试库、极小股票池）里「少 1 只」
#: 就被当成采集未完成。
_BAR_GAP_MIN = 100

#: 主动清理的规模上限：待清理行数 > max(50, 库内该日行数 × 5%) 时视为「行情完整性
#: 不可信」，**整轮拒绝**（不写不清），要求先重新采集。防止一次清空大半个交易日。
_CLEAR_MIN_ALLOWANCE = 50
_CLEAR_MAX_RATIO = 0.05

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


def _clear_cells(session, target: date, codes, columns) -> int:
    """把 `target` 当天这些 code 的指定列显式置 NULL，返回**实际改动的行数**。

    只删「本次重算判定已不可算」的单元格：`upsert_many_fill` 的 COALESCE 会把
    新 None 当成「这次没算」，从而把已失效的旧值一直留在库里。这里在写之前先清，
    让「缺失 / 待重算」有一个能读出来的状态（NULL），而不是冒充当前有效值。

    两点约束（2026-11-10 收紧）：

    - WHERE 里带 `OR(列 IS NOT NULL)`，**覆盖传入的每一列**：已经是 NULL 的行不再
      被 UPDATE，避免无意义写入 —— 每次无效 UPDATE 都会触发 `rps_daily` 的 UPDATE
      触发器、把 `rps` 域 revision 白推一格。
    - 返回值是**行数**（SQL `rowcount`），不是单元格数：一次 UPDATE 改一行里的多列
      也只算 1 行。日志里不得把它当单元格数用。
    """
    ordered = sorted(codes)
    if not ordered:
        return 0
    not_empty = or_(*[getattr(RpsDaily, name).is_not(None) for name in columns])
    changed = 0
    for start in range(0, len(ordered), _CLEAR_CHUNK):
        chunk = ordered[start : start + _CLEAR_CHUNK]
        result = session.execute(
            update(RpsDaily)
            .where(
                RpsDaily.trade_date == target,
                RpsDaily.code.in_(chunk),
                not_empty,
            )
            .values({name: None for name in columns})
        )
        changed += result.rowcount or 0
    return changed


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

        # 行情完整性第二道闸（数据驱动，不依赖「池子」这个外部口径）：目标日的 K 线
        # 行数不得显著少于**前 N 个交易日的中位数**。采集尚未完成、或分批导入跑到一半
        # 时，行数会明显偏少；此时把「今天没有这根 K 线」当成「历史行情被删除」去清空
        # 旧 RPS 是错的 —— 直接停跑，既不写也不清，要求先重新采集。
        baseline = estimate_pool_count(session, target, lookback=_BAR_BASELINE_DAYS)
        incomplete = (
            baseline
            and baseline - day_rows >= _BAR_GAP_MIN
            and day_rows < baseline * _BAR_COMPLETENESS
        )
        if incomplete:
            logger.warning(
                "RPS 停跑：%s 的日线只有 %d 行，比前 %d 个交易日中位数 %d 行少 %d 行"
                "（阈值：少 %d 行且低于 %.0f%%）—— 疑似采集未完成，拒绝写入与清理，先补 collect_kline",
                trade_date or target,
                day_rows,
                _BAR_BASELINE_DAYS,
                baseline,
                baseline - day_rows,
                _BAR_GAP_MIN,
                _BAR_COMPLETENESS * 100,
            )
            result = {
                "rows": 0,
                "trade_date": target,
                "skipped": "incomplete_bars",
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
        #: 当天**有 K 线**的票（哪怕一个窗口都算不出来）。用于识别过期单元格。
        day_codes: set[str] = set()
        current = ""
        rows: list[tuple[date, float]] = []

        def flush() -> None:
            if not current:
                return
            # 当天没有可用日线（停牌 / 漏采）→ 不参与当天横截面排名
            if not rows or rows[-1][0] != target:
                return
            day_codes.add(current)
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

        # ---- 过期 / 待重算：先清旧值，再写新值（同一事务，失败一起回滚） ----
        # 清理三类单元格，范围严格限定 `trade_date == target`：
        #   1. 当天有 K 线、但某窗口覆盖 <90% → 只清这一档的 ret/rps；
        #   2. 当天有 K 线、但一个窗口都算不出来 → 整行指标清空（保留主键行）；
        #   3. 库里有该日的行、但当天已经没有 K 线（行情被修订删掉）→ 整行清空。
        #
        # 破坏性操作前先量一遍规模：待清理行数超过上限时，说明「当天行情」与「库内
        # 已有结果」对不上，而单看行数无法区分「真被修订」与「采集/导入没跑完」。
        # 按保守原则**整轮拒绝**（不写也不清），把缺失状态留在库里，交给人重新采集。
        existing_codes = set(
            session.scalars(select(RpsDaily.code).where(RpsDaily.trade_date == target))
        )
        with_bar = day_codes
        without_bar = existing_codes - with_bar
        stale_rows = (with_bar - set(returns)) | without_bar
        allowance = max(_CLEAR_MIN_ALLOWANCE, int(len(existing_codes) * _CLEAR_MAX_RATIO))
        if len(stale_rows) > allowance:
            logger.warning(
                "RPS 停跑：%s 待清理 %d 行，超过上限 %d 行（库内 %d 行、当日 K 线 %d 行）"
                "—— 行情完整性不可信，拒绝破坏性清理且不写入，请先重新采集当日行情",
                target,
                len(stale_rows),
                allowance,
                len(existing_codes),
                day_rows,
            )
            result = {
                "rows": 0,
                "trade_date": target,
                "skipped": "stale_guard",
                "cost_seconds": round(time.monotonic() - started, 2),
            }
            if with_meta:
                result.update(
                    {
                        "pool": pool,
                        "pool_basis": pool_basis,
                        "denominators": {},
                        "day_rows": day_rows,
                        "stale_rows": len(stale_rows),
                    }
                )
            return result

        cleared_rows = _clear_cells(session, target, stale_rows, _METRIC_COLUMNS)
        for window in WINDOWS:
            stale_cells = {
                code for code, computed in returns.items() if computed[window] is None
            }
            if stale_cells:
                cleared_rows += _clear_cells(
                    session, target, stale_cells, (f"ret{window}", f"rps{window}")
                )

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

        # fill 语义：本次某窗口算不出来（None）时**不覆盖**库里已有的非空值；
        # 真正失效的旧值已在上面显式清空，不依赖 COALESCE 兜底。
        written = upsert_many_fill(session, RpsDaily, records)

    cost = time.monotonic() - started
    logger.info(
        "RPS 扫描 %s：%d 只（%s），清理过期行 %d 行，用时 %.1fs",
        target,
        written,
        " / ".join(f"{window} 日 {len(ordered[window])} 只" for window in WINDOWS),
        cleared_rows,
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

