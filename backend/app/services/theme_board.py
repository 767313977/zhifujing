"""今日主线题材看板（FEAT-THEME-BOARD，2026-10-31）。

纯聚合：涨停池 + `stock_concept`（开盘红精选板块）+ `sector_daily`。**不发外部请求**。

口径（与 `/sectors`、`/limit-up` 共用同一份聚合函数，避免两套涨停数）：

- 「主线」= 当日**涨停股共振题材**：同一精选板块下有 ≥ `themes.MIN_CLUSTER`（3）只涨停股；
  家数来自 `services.themes.limit_up_themes`，与板块页、简报完全同源；
- 一个票可以挂多个题材，所以各主线家数之和 ≥ 当日涨停总数 —— 这是口径本身的性质，
  页面同时显示 `limit_up_total` 让人能对上总量（验收要求「与 /limit-up 对得上」）；
- 龙头 / 中军 / 跟风（**可测的确定性规则**，不引入主观判断）：
  1. 龙头 = 连板数最高者；同板数比封单额，再同则代码小的在前；
  2. 中军 = 剩余票里**总市值最大**的一只（题材里的权重票）；
  3. 跟风 = 剩余票按（连板数、封单额）降序取前 2。
  三者互不重复；成员不足时能出几只出几只。
- 主线切换：与**上一交易日**的同名题材集合比较 —— 昨天不在今天在 = 新晋；
  两天都在 = 延续；昨天在、今天不在（或跌破共振门槛）= 退潮（放在 `fading` 里）。
  排名变化 = 昨日名次 − 今日名次（正数 = 上升）；新晋没有昨日名次，记 null。
"""

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import LimitPool, SectorDaily
from app.services.themes import limit_up_themes
from app.services.trading_days import previous_trade_day
from app.sources.kaipanhong import TAXONOMY_SELECTED
from app.timeutil import today_cn


def _latest_limit_day(session: Session) -> date | None:
    return session.scalar(
        select(func.max(LimitPool.trade_date)).where(LimitPool.pool_type == "up")
    )


def _pool_rows(session: Session, day: date) -> dict[str, LimitPool]:
    return {
        row.code: row
        for row in session.scalars(
            select(LimitPool).where(
                LimitPool.trade_date == day, LimitPool.pool_type == "up"
            )
        )
    }


def _sector_rows(session: Session, day: date) -> dict[str, SectorDaily]:
    return {
        row.name: row
        for row in session.scalars(
            select(SectorDaily).where(
                SectorDaily.trade_date == day,
                SectorDaily.taxonomy == TAXONOMY_SELECTED,
            )
        )
        if row.name
    }


def _leader_key(row: LimitPool) -> tuple:
    """龙头排序：连板数 → 封单额 → 代码。缺值一律当 0 / 最大字符串排后。"""
    return (-(row.consecutive or 0), -(row.seal_amount or 0), row.code)


def _pick_leaders(
    members: list[str], pool: dict[str, LimitPool]
) -> list[dict[str, Any]]:
    """按模块说明里的规则给题材成员分「龙头 / 中军 / 跟风」。"""
    rows = [pool[code] for code in members if code in pool]
    if not rows:
        return []
    rows.sort(key=_leader_key)
    leaders: list[dict[str, Any]] = []

    def add(row: LimitPool, role: str) -> None:
        leaders.append(
            {
                "code": row.code,
                "name": row.name,
                "role": role,
                "consecutive": row.consecutive,
                "seal_amount": row.seal_amount,
                "total_mv": row.total_mv,
            }
        )

    add(rows[0], "龙头")
    rest = rows[1:]
    if rest:
        # 中军：总市值最大；缺市值时退到流通市值、再退到成交额
        core = max(
            rest,
            key=lambda row: (
                row.total_mv or 0,
                row.float_mv or 0,
                row.amount or 0,
                -len(row.code),
            ),
        )
        add(core, "中军")
        rest = [row for row in rest if row is not core]
    for row in rest[:2]:
        add(row, "跟风")
    return leaders


def _rank_map(clusters) -> dict[str, int]:
    """题材 → 名次（1 起）。`limit_up_themes` 已按家数、涨幅排好序。"""
    return {cluster.concept: index + 1 for index, cluster in enumerate(clusters)}


def board(session: Session, trade_date: date | None = None) -> dict[str, Any]:
    """某交易日的主线看板。没有涨停数据时返回空行 + 说明，不抛错。"""
    day = trade_date or _latest_limit_day(session) or today_cn()
    prev = previous_trade_day(day, session)
    pool = _pool_rows(session, day)
    clusters, by_stock = limit_up_themes(session, day)
    sectors = _sector_rows(session, day)

    prev_clusters = limit_up_themes(session, prev)[0] if prev is not None else []
    prev_rank = _rank_map(prev_clusters)

    rank = _rank_map(clusters)
    today_set = set(rank)
    rows: list[dict[str, Any]] = []
    for cluster in clusters:
        sector = sectors.get(cluster.concept)
        members = [
            code for code, concepts in by_stock.items() if cluster.concept in concepts
        ]
        leaders = _pick_leaders(members, pool)
        consecutive = [
            (pool[code].consecutive or 1) for code in members if code in pool
        ]
        rows.append(
            {
                "concept": cluster.concept,
                "rank": rank[cluster.concept],
                "prev_rank": prev_rank.get(cluster.concept),
                "rank_change": (
                    prev_rank[cluster.concept] - rank[cluster.concept]
                    if cluster.concept in prev_rank
                    else None
                ),
                "state": "延续" if cluster.concept in prev_rank else "新晋",
                "limit_up_count": cluster.count,
                "max_consecutive": max(consecutive) if consecutive else None,
                "pct_chg": cluster.pct_chg,
                "net_inflow": sector.net_inflow if sector is not None else None,
                "leaders": leaders,
            }
        )

    # 没有当天涨停数据时**不判退潮**：那只是「没采到 / 非交易日」，不是题材退潮
    fading = (
        [
            {
                "concept": cluster.concept,
                "prev_rank": prev_rank.get(cluster.concept),
                "prev_count": cluster.count,
                "pct_chg": cluster.pct_chg,
            }
            for cluster in prev_clusters
            if cluster.concept not in today_set
        ]
        if pool
        else []
    )

    if not pool:
        note = "当日没有涨停数据（非交易日，或这一天的采集还没跑）。"
    elif not rows:
        note = "当日涨停股里没有形成共振的题材（同一精选板块 ≥3 只才算主线）。"
    else:
        note = None

    return {
        "trade_date": day,
        "prev_trade_date": prev,
        "limit_up_total": len(pool),
        "rows": rows,
        "fading": fading,
        "note": note,
    }
