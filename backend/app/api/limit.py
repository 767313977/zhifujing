"""涨停 / 跌停 / 炸板 与龙虎榜接口。"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import resolve_trade_date
from app.db import get_db
from app.models import Lhb, LimitPool, LimitReason, StockConcept, TradeCalendar
from app.schemas import (
    LadderLevel,
    LhbOut,
    LimitPoolOut,
    LimitStock,
    LimitStockTheme,
    LimitThemeItem,
    LimitThemes,
    PromotionLevel,
    PromotionSeries,
)
from app.services.themes import limit_up_themes

router = APIRouter(prefix="/api", tags=["复盘"])

logger = logging.getLogger(__name__)

POOL_TYPES = {"up", "down", "broken"}

# 晋级率展示的档位：1进2 / 2进3 / 3进4 / 4进5
PROMOTION_LEVELS = (1, 2, 3, 4)


def _rate(promoted: int, total: int) -> float | None:
    """昨日该档无票时返回 None，而不是 0 —— 0 会被读成「全军覆没」。"""
    if total == 0:
        return None
    return round(promoted / total * 100, 1)


def _boards(session: Session, trade_date: date) -> dict[str, str]:
    """当日每只涨停股的**开盘啦精选板块**名（涨停天梯的落库结果）。

    梯队上的分类标签原本用的是 `limit_pool.industry`，那是 iFinD 的**同花顺行业**
    （半导体 / 家居用品 / 铁路公路），与开盘啦 App 那张天梯不是一个口径 ——
    同一只票会给出完全不同的词（博通集成：半导体 vs 芯片；三羊马：铁路公路 vs
    智能驾驶）。短线看的是后者，所以改成读这张表。

    只覆盖涨停股，且实测每只票恰好一个板块（09-21：101 只各 1 条，涨停池 103 只
    里有 2 只没归到板块）。**没归到的不编**，留空。

    ⚠️ `stock_concept` 的主键含 `concept`，所以**「恰好一个」只是实测、schema 层面
    不保证**。一只票同一天有多个板块时按 `concept` 排序取首个并告警 —— 原来是
    `dict(...)` 直接覆盖，取到哪一个取决于行序，既不稳定也不留痕（2026-09-27 修）。
    """
    rows = session.execute(
        select(StockConcept.code, StockConcept.concept)
        .where(StockConcept.trade_date == trade_date)
        .order_by(StockConcept.code, StockConcept.concept)
    ).all()
    boards: dict[str, str] = {}
    multi: list[str] = []
    for code, concept in rows:
        if code in boards:
            multi.append(code)
            continue
        boards[code] = concept
    if multi:
        logger.warning(
            "%s 有 %d 只票归到了多个开盘啦板块（%s…），每只只取排序首个",
            trade_date,
            len(multi),
            "、".join(multi[:3]),
        )
    return boards


def _reasons(session: Session, trade_date: date) -> dict[str, str]:
    """当日每只涨停股的**涨停原因**（同花顺 `reason_type`，见 `sources/ths_limit_up.py`）。

    与 `_boards` 是两个来源、两个口径：这个答「为什么涨停」（房地产+城市更新+北京国资），
    那个答「属于哪个开盘啦精选板块」（地产链）。覆盖度也略有差别 —— 同花顺那边少
    ST / 北交所几只，对不上的票留空。
    """
    return dict(
        session.execute(
            select(LimitReason.code, LimitReason.reason).where(
                LimitReason.trade_date == trade_date
            )
        ).all()
    )


def _to_stock(
    row: LimitPool,
    boards: dict[str, str],
    reasons: dict[str, str],
    *,
    consecutive: int | None = None,
) -> LimitStock:
    """开盘啦板块名与涨停原因都不在 `LimitPool` 里（那张表是东财口径），校验后补进去。

    `consecutive` 传了就以传入值为准：天梯按 `consecutive or 1` 分层、明细直接读原字段，
    不统一的话同一只票会「在天梯里是 1 板、在明细里是 —」（2026-09-27 修）。
    """
    return LimitStock.model_validate(row).model_copy(
        update={
            "board": boards.get(row.code),
            "reason": reasons.get(row.code),
            "consecutive": row.consecutive if consecutive is None else consecutive,
        }
    )


def _build_ladder(
    rows: list[LimitPool], boards: dict[str, str], reasons: dict[str, str]
) -> list[LadderLevel]:
    """按连板高度分层，高度从高到低。"""
    buckets: dict[int, list[LimitPool]] = {}
    for row in rows:
        buckets.setdefault(row.consecutive or 1, []).append(row)
    return [
        LadderLevel(
            consecutive=level,
            count=len(items),
            # 明细也按这一层的层号显示（原字段为空的票否则会显示成「—」）
            stocks=[
                _to_stock(item, boards, reasons, consecutive=level) for item in items
            ],
        )
        for level, items in sorted(buckets.items(), reverse=True)
    ]


@router.get("/limit/pool", response_model=LimitPoolOut)
def limit_pool(
    pool_type: str = Query("up", alias="type", description="up=涨停 down=跌停 broken=炸板"),
    trade_date: date = Depends(resolve_trade_date),
    session: Session = Depends(get_db),
) -> LimitPoolOut:
    if pool_type not in POOL_TYPES:
        raise HTTPException(
            status_code=400, detail=f"type 只能是 {sorted(POOL_TYPES)}，收到 {pool_type}"
        )

    rows = list(
        session.scalars(
            select(LimitPool)
            .where(LimitPool.trade_date == trade_date, LimitPool.pool_type == pool_type)
            # 连板高度优先，其次封板资金（跌停池与炸板池的这两列可能为空）
            .order_by(
                LimitPool.consecutive.desc().nullslast(),
                LimitPool.seal_amount.desc().nullslast(),
            )
        )
    )
    # 只要涨停池查这两张表 —— `stock_concept` 与 `limit_reason` 都只覆盖涨停股，
    # 跌停 / 炸板池本来就没有对应数据
    if pool_type == "up":
        boards = _boards(session, trade_date)
        reasons = _reasons(session, trade_date)
    else:
        boards, reasons = {}, {}
    return LimitPoolOut(
        trade_date=trade_date,
        pool_type=pool_type,
        total=len(rows),
        # 梯队只对涨停有意义：跌停是「连续跌停」、炸板池没有连板数
        ladder=_build_ladder(rows, boards, reasons) if pool_type == "up" else None,
        stocks=[_to_stock(row, boards, reasons) for row in rows],
    )


@router.get("/lhb", response_model=list[LhbOut])
def lhb_list(
    trade_date: date | None = Query(None, alias="date"),
    session: Session = Depends(get_db),
) -> list[LhbOut]:
    """龙虎榜：**缺省回落到「最近有龙虎榜的那一天」**，而不是 `resolve_trade_date`。

    为什么不用那个公共依赖（2026-09-30 修）：它缺省取**库中最新情绪日**，而龙虎榜是
    「按日期问」的来源 —— 交易所当晚才发布，15:05 那一趟问不到、第二天也不替它补。
    结果两边的日期一旦错开（实测 09-29 访问 / 龙虎榜 只到 09-28），条件永远不成立、
    接口永远返回空数组，页面上就是「龙虎榜 0 条」，看着像故障。

    现在缺省取 `max(trade_date)`，返回的每条都带 `trade_date`，前端把它标出来
    （`LhbTable` title 里显示实际数据日），所以「为什么不是今天」是自解释的。

    表里一条都没有时返回 `[]`（而不是 404）：前端本来就是按空列表渲染的。
    """
    if trade_date is None:
        trade_date = session.scalar(select(func.max(Lhb.trade_date)))
        if trade_date is None:
            return []
    rows = list(
        session.scalars(
            select(Lhb)
            .where(Lhb.trade_date == trade_date)
            .order_by(Lhb.net_buy.desc().nullslast())
        )
    )
    return [LhbOut.model_validate(row) for row in rows]


@router.get("/limit/themes", response_model=LimitThemes)
def limit_themes(
    trade_date: date = Depends(resolve_trade_date),
    session: Session = Depends(get_db),
) -> LimitThemes:
    """涨停股的题材归属：哪些题材聚了最多涨停股，以及每只票挂的题材。

    聚合口径见 `app.services.themes.limit_up_themes`（板块页与飞书简报共用一份）。
    """
    clusters, by_stock = limit_up_themes(session, trade_date)
    return LimitThemes(
        trade_date=trade_date,
        clusters=[
            LimitThemeItem(
                concept=item.concept, count=item.count, pct_chg=item.pct_chg
            )
            for item in clusters
        ],
        stocks=[
            LimitStockTheme(code=code, themes=themes)
            for code, themes in by_stock.items()
        ],
    )


@router.get("/limit/promotion", response_model=PromotionSeries)
def promotion(
    days: int = Query(15, ge=2, le=60, description="返回最近 N 个交易日的晋级率"),
    session: Session = Depends(get_db),
) -> PromotionSeries:
    """连板晋级率：昨日 N 板股今日晋级到 (N+1) 板的比例。

    这是打板复盘的核心指标 —— 它衡量「昨天的高度能不能被今天接住」。
    晋级率同步走高说明资金愿意接力，快速回落说明高位股开始被抛弃。

    注意：算某天的晋级率需要**它前一日**的涨停池作基数，所以 N 天的池子
    只能得出 N-1 天的晋级率。窗口内最早那天只当基数、不出现在结果里，
    这样不会因为「前一日无数据」而算出一堆假的 0%。
    """
    # 日期序列取**交易日历**，不取 limit_pool 自己的日期（2026-10-10 修）：
    # 池子缺一天时（回补没跑到、上游那天没数据），原来会把「T-2」当成「昨日」，
    # 于是 T-1 那批的晋级率被算到 T 上，整条曲线错位。用日历就没有这个问题 ——
    # 缺的那天池子是空的，基数 0、晋级率记 None（如实呈现「那天没数据」）。
    anchor = session.scalar(
        select(func.max(LimitPool.trade_date)).where(LimitPool.pool_type == "up")
    )
    if anchor is None:
        return PromotionSeries(
            dates=[], levels=[], overall_counts=[], overall_promoted=[], overall_rates=[]
        )
    recent = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date <= anchor)
            .order_by(TradeCalendar.trade_date.desc())
            .limit(days + 1)
        )
    )
    if len(recent) < 2:
        return PromotionSeries(
            dates=[], levels=[], overall_counts=[], overall_promoted=[], overall_rates=[]
        )

    dates = sorted(recent)
    rows = session.execute(
        select(LimitPool.trade_date, LimitPool.code, LimitPool.consecutive).where(
            LimitPool.trade_date.in_(dates), LimitPool.pool_type == "up"
        )
    ).all()

    # {日期: {代码: 连板数}}，连板数为空时按首板处理
    pools: dict[date, dict[str, int]] = {day: {} for day in dates}
    for day, code, consecutive in rows:
        pools[day][code] = consecutive or 1

    levels: list[PromotionLevel] = []
    for level in PROMOTION_LEVELS:
        counts: list[int] = []
        promoted: list[int] = []
        rates: list[float | None] = []
        for index in range(1, len(dates)):
            previous, today = pools[dates[index - 1]], pools[dates[index]]
            base = [code for code, value in previous.items() if value == level]
            hit = sum(1 for code in base if today.get(code) == level + 1)
            counts.append(len(base))
            promoted.append(hit)
            rates.append(_rate(hit, len(base)))
        levels.append(
            PromotionLevel(
                level=level,
                label=f"{level}进{level + 1}",
                counts=counts,
                promoted=promoted,
                rates=rates,
            )
        )

    overall_counts: list[int] = []
    overall_promoted: list[int] = []
    overall_rates: list[float | None] = []
    for index in range(1, len(dates)):
        previous, today = pools[dates[index - 1]], pools[dates[index]]
        hit = sum(1 for code, value in previous.items() if today.get(code) == value + 1)
        overall_counts.append(len(previous))
        overall_promoted.append(hit)
        overall_rates.append(_rate(hit, len(previous)))

    return PromotionSeries(
        dates=dates[1:],
        levels=levels,
        overall_counts=overall_counts,
        overall_promoted=overall_promoted,
        overall_rates=overall_rates,
    )
