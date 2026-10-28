"""板块 RPS：板块 N 日涨幅在**当天所有板块**里的百分位（中位秩）。

## 数据源为什么是 `sector_daily` 而不是 `sector_member`

`sector_member` 只有「当天」的成分快照（打开板块时现取），拿它回看历史就是前视偏差：
今天被划进热点的票，去年并不在那个板块里。而 `sector_daily.pct_chg` 是开盘红**每天
自己算的板块涨跌幅**（成分当日生效），本身就带历史，不存在这个问题 —— 所以板块
强度只用它，不自己拼成分。

## 口径与个股 RPS 完全一致

复用 `services/rps.py`：`pct_chg` 复利、窗口按交易日截、中位秩百分位。区别只有
「横截面」从个股换成了板块（精选板块 279 个 / 行业 104 个，量纲不可跨口径比）。

## 这套东西**不做选股**，只做「板块温度」

2026-10-25 已补齐 `sector_member` 的 60 天逐日成分（`scripts/backfill_sector_members.py`），
并做过「板块强度 → 个股」的三种检验（`docs/回测_板块到个股_20261025.md`）：

| 检验 | 结果 |
| --- | --- |
| IC（板块 RPS20 → 个股 D+1） | −0.042（t=−1.7），五分位收益**单调递减** |
| 强势板块（RPS20≥90）× 每板块涨幅前 3 | 超额 −0.11% |
| 强势板块 × 突破形态 | 超额 −0.30%（全市场突破是 **+0.18%**） |

所以仍然**不做**选股功能：叠板块强度会削弱本来有效的突破信号。只把 `rps20_on` 留作
研究口径，供 `scripts/backtest_sector_stock.py` 复用。
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import SectorDaily, TradeCalendar
from app.services import rps

logger = logging.getLogger(__name__)

#: 两个窗口（交易日）
WINDOWS = (5, 20)

#: 板块口径：精选板块（短线口径）与行业（宽基口径）**不能混着比**
TAXONOMIES = ("kph_selected", "kph_industry")


@dataclass(frozen=True, slots=True)
class SectorRpsRow:
    code: str
    name: str | None
    ret5: float | None
    ret20: float | None
    rps5: float | None
    rps20: float | None
    member_count: int | None
    leader_name: str | None
    leader_pct_chg: float | None
    net_inflow: float | None


@dataclass(frozen=True, slots=True)
class SectorRpsBoard:
    trade_date: date | None
    taxonomy: str
    rows: list[SectorRpsRow] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SectorTrackDay:
    trade_date: date
    accepted: int
    ret5: float | None
    excess5: float | None
    ret20: float | None
    excess20: float | None
    #: `ok` = 前面的窗口和后面的持有期都有数据；`pending` = 持有期还没走完
    status: str


@dataclass(frozen=True, slots=True)
class SectorTrack:
    trade_date: date | None
    days: list[SectorTrackDay] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


def _window_rows(
    session: Session, target: date, taxonomy: str
) -> tuple[dict[int, date], list[tuple]]:
    """今天的窗口起点 + 窗口内全部板块行。"""
    days = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date <= target)
            .order_by(TradeCalendar.trade_date.desc())
            .limit(max(WINDOWS))
        )
    )
    if not days:
        return {}, []
    starts = {window: days[min(window, len(days)) - 1] for window in WINDOWS}
    rows = session.execute(
        select(
            SectorDaily.sector_code,
            SectorDaily.name,
            SectorDaily.trade_date,
            SectorDaily.pct_chg,
            SectorDaily.member_count,
            SectorDaily.leader_name,
            SectorDaily.leader_pct_chg,
            SectorDaily.net_inflow,
        ).where(
            SectorDaily.taxonomy == taxonomy,
            SectorDaily.trade_date >= min(starts.values()),
            SectorDaily.trade_date <= target,
        )
    ).all()
    return starts, rows


def _latest_meta(rows: list[tuple], target: date) -> dict[str, tuple]:
    """每个板块**最近一行**的展示信息（名称 / 成分数 / 龙一 / 净流入）。

    优先取当天那一行；当天没有的（板块当天没被采集到）退到窗口内最近一次，
    否则 `name` 会是 None —— 榜单上出现一个没名字的板块比缺字段更糟。
    """
    latest_day: dict[str, date] = {}
    meta: dict[str, tuple] = {}
    for code, name, day, _pct, members, leader, leader_pct, inflow in rows:
        if day == target:
            meta[code] = (name, members, leader, leader_pct, inflow)
            latest_day[code] = day
    for code, name, day, _pct, members, leader, leader_pct, inflow in rows:
        if code in meta:
            continue
        if code not in latest_day or day > latest_day[code]:
            latest_day[code] = day
            meta[code] = (name, members, leader, leader_pct, inflow)
    return meta


def _returns(rows: list[tuple], starts: dict[int, date]) -> dict[str, dict[int, float]]:
    """`{板块: {窗口: 涨幅%}}`。"""
    series: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for code, _name, day, pct, _members, _leader, _leader_pct, _inflow in rows:
        if pct is not None:
            series[code].append((day, float(pct)))
    out: dict[str, dict[int, float]] = {}
    for code, values in series.items():
        computed = {
            window: rps.ret(rps.window_values(values, starts[window]), window)
            for window in WINDOWS
        }
        if any(value is not None for value in computed.values()):
            out[code] = computed
    return out


def _percentiles(values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(values.values())
    return {code: rps.mid_rank(ordered, value) for code, value in values.items()}


def rps20_on(session: Session, target: date, taxonomy: str = "kph_selected") -> dict[str, float]:
    """某天各板块的 RPS20（`{板块代码: 0~100}`）。

    「板块 → 个股」的回测与页面用**同一份**口径：20 日复利涨幅、按交易日截窗口、
    中位秩分位；`taxonomy` 两个口径不能混比。
    """
    starts, rows = _window_rows(session, target, taxonomy)
    if not rows:
        return {}
    returns = _returns(rows, starts)
    values = {code: value[20] for code, value in returns.items() if value[20] is not None}
    return _percentiles(values)


def board(
    session: Session,
    trade_date: date | None = None,
    taxonomy: str = "kph_selected",
    limit: int = 30,
) -> SectorRpsBoard:
    """板块强度榜：按 20 日 RPS 降序。`taxonomy` 两个口径不能混比。"""
    target = trade_date or session.scalar(
        select(func.max(SectorDaily.trade_date)).where(SectorDaily.taxonomy == taxonomy)
    )
    if target is None:
        return SectorRpsBoard(trade_date=None, taxonomy=taxonomy)

    starts, rows = _window_rows(session, target, taxonomy)
    if not rows:
        return SectorRpsBoard(trade_date=target, taxonomy=taxonomy)

    returns = _returns(rows, starts)
    meta = _latest_meta(rows, target)
    percentiles = {
        window: _percentiles(
            {code: value[window] for code, value in returns.items() if value[window] is not None}
        )
        for window in WINDOWS
    }

    picked: list[SectorRpsRow] = []
    for code, computed in returns.items():
        name, members, leader, leader_pct, inflow = meta.get(code, (None, None, None, None, None))
        picked.append(
            SectorRpsRow(
                code=code,
                name=name,
                ret5=computed[5],
                ret20=computed[20],
                rps5=percentiles[5].get(code),
                rps20=percentiles[20].get(code),
                member_count=members,
                leader_name=leader,
                leader_pct_chg=leader_pct,
                net_inflow=inflow,
            )
        )
    # 缺 20 日值的（新板块）排在最后
    picked.sort(key=lambda item: (item.rps20 is None, -(item.rps20 or 0), item.code))
    return SectorRpsBoard(trade_date=target, taxonomy=taxonomy, rows=picked[:limit])


def _forward_returns(
    session: Session, day: date, horizons: tuple[int, ...], taxonomy: str
) -> dict[int, dict[str, float]]:
    """`day` 之后第 h 个交易日的板块涨幅（`pct_chg` 复利，从 day+1 起算）。

    缺那几天的板块不出现在结果里（没有行就没法复利）。

    ⚠️ 「缺行」必须**按行数判**（2026-10-28 体检）：窗口 `day+1 … exits[h]` 里本来
    正好有 h 个交易日，所以只有拿到 h 行才算走完。原来只按 `when <= exit_day` 累加、
    不校验条数 —— 某板块 5 日窗口只采到 2 行时也给一个「5 日收益」，它既会混进横截面
    去比「最强板块」，又会污染 `track()` 的等权基准（缺的若是大跌日，它反而被选进
    top5）。docstring 早就写着「缺那几天的板块不出现」，代码却没落实。
    """
    exits_candidates = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date > day)
            .order_by(TradeCalendar.trade_date)
            .limit(max(horizons))
        )
    )
    # ⚠️ 交易日历**预置到年底（含未来日期）**，不能拿它当「已有数据」的边界
    #（2026-10-28 体检）：日历里第 5 个交易日可能是下周，而 `sector_daily` 只到前天 ——
    # 原来的 exits 会指向未来日，于是「5 日收益」实际只累积了 1~2 天。
    # 这里用当日可用的最后一个板块数据日封顶，走不完的 horizon 直接不给值。
    data_end = session.scalar(
        select(func.max(SectorDaily.trade_date)).where(SectorDaily.taxonomy == taxonomy)
    )
    exits = {
        h: exits_candidates[h - 1]
        for h in horizons
        if h <= len(exits_candidates)
        and (data_end is None or exits_candidates[h - 1] <= data_end)
    }
    if not exits:
        return {}

    last = max(exits.values())
    series: dict[int, dict[str, list[float]]] = {h: defaultdict(list) for h in exits}
    for code, when, pct in session.execute(
        select(SectorDaily.sector_code, SectorDaily.trade_date, SectorDaily.pct_chg)
        .where(
            SectorDaily.taxonomy == taxonomy,
            SectorDaily.trade_date > day,
            SectorDaily.trade_date <= last,
            SectorDaily.pct_chg.is_not(None),
        )
        .order_by(SectorDaily.sector_code, SectorDaily.trade_date)
    ):
        for h, exit_day in exits.items():
            if when <= exit_day:
                series[h][code].append(float(pct))

    out: dict[int, dict[str, float]] = {}
    for h, per_code in series.items():
        out[h] = {}
        for code, values in per_code.items():
            # 只有走满 h 个交易日才算数；缺行的板块该档记缺失（不进均值、不进基准）
            if len(values) != h:
                continue
            factor = 1.0
            for pct in values:
                factor *= 1 + pct / 100
            out[h][code] = round((factor - 1) * 100, 4)
    return out


def track(
    session: Session,
    days: int = 20,
    top: int = 5,
    taxonomy: str = "kph_selected",
) -> SectorTrack:
    """板块动量的延续性：买「RPS 最高的 `top` 个板块」比买全部板块的平均更好吗。

    ⚠️ 收益是**板块指数**的收益（成分当日生效），不是个股 —— 它回答的是「板块层面
    有没有动量」，不能直接当成「买板块里的票能赚这么多」（还要扣选股与交易成本）。
    """
    target = session.scalar(
        select(func.max(SectorDaily.trade_date)).where(SectorDaily.taxonomy == taxonomy)
    )
    if target is None:
        return SectorTrack(trade_date=None)
    days_with_data = sorted(
        session.scalars(
            select(SectorDaily.trade_date)
            .where(SectorDaily.taxonomy == taxonomy)
            .group_by(SectorDaily.trade_date)
            .order_by(SectorDaily.trade_date.desc())
            .limit(days)
        )
    )

    day_rows: list[SectorTrackDay] = []
    pooled: dict[int, list[tuple[float, float]]] = {5: [], 20: []}
    for day in days_with_data:
        starts, rows = _window_rows(session, day, taxonomy)
        returns = _returns(rows, starts)
        scored = {
            code: value[20] for code, value in returns.items() if value[20] is not None
        }
        if len(scored) < top:
            continue
        ranked = [code for code, _ in sorted(scored.items(), key=lambda item: item[1], reverse=True)[:top]]
        forward = _forward_returns(session, day, (5, 20), taxonomy)

        ret5 = ret20 = excess5 = excess20 = None
        accepted = 0
        for h in (5, 20):
            per_sector = forward.get(h) or {}
            values = [per_sector[code] for code in ranked if code in per_sector]
            if not values:
                continue
            market = sum(per_sector.values()) / len(per_sector)
            mean = sum(values) / len(values)
            if h == 5:
                ret5 = round(mean, 2)
                excess5 = round(mean - market, 2)
            else:
                ret20 = round(mean, 2)
                excess20 = round(mean - market, 2)
            pooled[h].extend((value, market) for value in values)
            accepted = max(accepted, len(values))

        day_rows.append(
            SectorTrackDay(
                day,
                accepted,
                ret5,
                excess5,
                ret20,
                excess20,
                "ok" if ret5 is not None else "pending",
            )
        )

    def _summary(values: list[tuple[float, float]]) -> dict:
        if not values:
            return {"samples": 0, "mean": None, "excess": None}
        means = [value for value, _ in values]
        excesses = [value - market for value, market in values]
        return {
            "samples": len(values),
            "mean": round(sum(means) / len(means), 2),
            "excess": round(sum(excesses) / len(excesses), 2),
        }

    return SectorTrack(
        trade_date=target,
        days=day_rows,
        summary={
            "days": len(day_rows),
            "verified_days": sum(1 for row in day_rows if row.status == "ok"),
            "top": top,
            "ret5": _summary(pooled[5]),
            "ret20": _summary(pooled[20]),
        },
    )
