"""形态选股的查询接口。

**一次全取、前端筛**：命中量每天几百条，接口一次把当天全部返回，前端自己按
形态与分数切 —— 沿用板块页 `/ranking` 的做法（那里一次返回 465 个板块给前端
反复切排序）。拖滑块就发一次请求是没必要的，而且筛选逻辑放前端，
「标签上的家数」和「筛出来的行数」天然不会打架。
"""

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import PatternHit, StockDaily, StockUniverse, TemplatePool, TradeCalendar
from app.schemas import (
    PatternCount,
    PatternHitItem,
    PatternMeta,
    PatternStockOut,
    PatternSummary,
    TemplateBoard,
    TemplateItem,
)
from app.services.patterns import PATTERNS

router = APIRouter(prefix="/api/patterns", tags=["patterns"])

_META = {pattern.key: pattern for pattern in PATTERNS}


def _latest_date(db: Session) -> date | None:
    """最近一个**有命中记录**的交易日。

    不能用「最近交易日」：当天的扫描要等日线采完才跑，用日历日期当默认值会
    让页面在收盘后到扫描完成之间显示空白 —— 看起来像坏了，其实只是还没到点。
    """
    return db.scalar(select(func.max(PatternHit.trade_date)))


@router.get("/catalog", response_model=list[PatternMeta])
def catalog() -> list[PatternMeta]:
    """形态清单，前端据此生成分组筛选条。"""
    return [
        PatternMeta(key=p.key, name=p.name, group=p.group) for p in PATTERNS
    ]


@router.get("/hits", response_model=list[PatternStockOut])
def hits(
    db: Session = Depends(get_db),
    trade_date: date | None = Query(None, alias="date"),
    min_score: float = Query(0.0, ge=0.0, le=100.0),
    limit: int = Query(500, ge=1, le=3000),
) -> list[PatternStockOut]:
    """某交易日的全部命中，**按股票归并**（一只票命中多个形态就是一行多标签）。"""
    target = trade_date or _latest_date(db)
    if target is None:
        return []

    rows = db.scalars(
        select(PatternHit)
        .where(PatternHit.trade_date == target, PatternHit.score >= min_score)
        .order_by(PatternHit.score.desc())
    ).all()
    if not rows:
        return []

    # 日均成交额与市值是**股票的属性**，从股票池取；命中记录里不存这两项。
    # 少数票可能已经跌出池子（流动性降到门槛以下），那时给 null 而不是 0 ——
    # 「没有数据」和「是 0」是两件事
    pool = {
        code: (avg_amount, total_mv)
        for code, avg_amount, total_mv in db.execute(
            select(StockUniverse.code, StockUniverse.avg_amount, StockUniverse.total_mv).where(
                StockUniverse.code.in_({row.code for row in rows})
            )
        ).all()
    }

    grouped: dict[str, PatternStockOut] = {}
    for row in rows:
        meta = _META.get(row.pattern)
        item = PatternHitItem(
            pattern=row.pattern,
            # 形态清单里没有的 key 只可能是旧数据（改过 key），照原样显示不丢信息
            pattern_name=meta.name if meta else row.pattern,
            group=meta.group if meta else "其他",
            score=row.score,
            key_levels=row.key_levels or {},
            detail=row.detail or {},
        )
        existing = grouped.get(row.code)
        if existing is None:
            avg_amount, total_mv = pool.get(row.code, (None, None))
            grouped[row.code] = PatternStockOut(
                code=row.code,
                name=row.name,
                trade_date=row.trade_date,
                close=row.close,
                pct_chg=row.pct_chg,
                amount=row.amount,
                avg_amount=avg_amount,
                total_mv=total_mv,
                score=row.score,
                patterns=[item],
            )
        else:
            existing.patterns.append(item)
            # 已经按分数降序取过，所以第一条就是最高分；这里只在必要时提升
            existing.score = max(existing.score, row.score)

    result = sorted(grouped.values(), key=lambda stock: stock.score, reverse=True)
    return result[:limit]


@router.get("/summary", response_model=PatternSummary)
def summary(
    db: Session = Depends(get_db),
    trade_date: date | None = Query(None, alias="date"),
) -> PatternSummary:
    """各形态的命中家数，给筛选条的徽标、首页面板与飞书简报用。"""
    target = trade_date or _latest_date(db)
    if target is None:
        return PatternSummary(trade_date=None, total_hits=0, total_stocks=0, by_pattern=[])

    rows = db.execute(
        select(PatternHit.pattern, func.count(), func.count(func.distinct(PatternHit.code)))
        .where(PatternHit.trade_date == target)
        .group_by(PatternHit.pattern)
    ).all()
    counts = {pattern: (hits, stocks) for pattern, hits, stocks in rows}

    # 按形态清单的顺序输出，命中为 0 的也留着 —— 筛选条上少一个按钮比显示 0 更让人困惑
    by_pattern = [
        PatternCount(
            pattern=p.key,
            pattern_name=p.name,
            group=p.group,
            stocks=counts.get(p.key, (0, 0))[1],
        )
        for p in PATTERNS
    ]
    return PatternSummary(
        trade_date=target,
        total_hits=sum(hits for hits, _ in counts.values()),
        total_stocks=db.scalar(
            select(func.count(func.distinct(PatternHit.code))).where(
                PatternHit.trade_date == target
            )
        )
        or 0,
        by_pattern=by_pattern,
    )


# ---------------------------------------------------------------------- 样板池

# 「兜底线」= 今高 × 这个系数：次日收盘站上它才算站住（原文「收盘 ≥ 昨高 × 约 0.98」）
TEMPLATE_FLOOR_RATIO = 0.98

# 次日的日线结论
NEXT_STARTED = "started"  # 过线了，且收盘站上兜底线
NEXT_WEAK = "weak"  # 过线了，但收盘没站住
NEXT_MISSED = "missed"  # 连最高价都没过线


def _latest_template_date(db: Session) -> date | None:
    """最近一个**有样板记录**的交易日。

    与 `_latest_date` 同理，不用「最近交易日」：扫描要等日线采完才跑，
    用日历日期会让页面在收盘后到扫描完成之间显示空白 —— 看起来像坏了。
    """
    return db.scalar(select(func.max(TemplatePool.trade_date)))


def _next_bars(db: Session, day: date) -> tuple[date | None, dict[str, StockDaily]]:
    """样板日之后的下一个交易日，以及那天的日线（按代码索引）。

    两个坑都在这里挡掉了：

    1. **交易日历预置到年底**，所以「下一个交易日」必须再卡一个「不晚于今天」——
       否则会取出一个还没发生的日期，面板上就会出现「次日 2026-12-31」。
    2. 整表读当天日线再在内存里映射，**不要写 `IN(几百个代码)`**：SQLite 的
       绑定参数个数有上限（老版本 999），池子一大就把查询打挂（同 `api/funds.py`）。
    """
    next_day = db.scalar(
        select(TradeCalendar.trade_date)
        .where(TradeCalendar.trade_date > day, TradeCalendar.trade_date <= date.today())
        .order_by(TradeCalendar.trade_date)
        .limit(1)
    )
    if next_day is None:
        return None, {}
    rows = db.scalars(select(StockDaily).where(StockDaily.trade_date == next_day)).all()
    return next_day, {row.code: row for row in rows}


def _next_result(
    row: TemplatePool, bar: StockDaily | None, floor: float | None
) -> str | None:
    """次日的日线结论。判不了（次日还没到 / 停牌）给 None。

    只判日线能判的三条：过线（次日最高 > 今高）、收盘站上兜底线、收红。

    **量比 ≥ 1.5 刻意不判。** 它是「全天成交量」口径，而买点在「盘中刚过线」
    那一刻 —— 两者时间错配：过线的瞬间全天量还没走完，等量比真到 1.5 往往
    已经离买点很远了。所以这条确认只能盘中做，面板上必须写明它不在回看范围里
    （见设计文档 8.49.1）。
    """
    if bar is None or row.high is None or row.high <= 0 or bar.high is None:
        return None
    if bar.high <= row.high:
        return NEXT_MISSED
    if floor is not None and bar.close is not None and bar.close >= floor and (bar.pct_chg or 0) > 0:
        return NEXT_STARTED
    return NEXT_WEAK


@router.get("/template", response_model=TemplateBoard)
def template_board(
    db: Session = Depends(get_db),
    trade_date: date | None = Query(None, alias="date"),
) -> TemplateBoard:
    """样板池：次日「明天盯」清单 + 次日的过线结果。

    一次全取、前端筛 —— 数量级与形态命中同档（每天几十到几百只），
    拖排序不该反复打接口。
    """
    day = trade_date or _latest_template_date(db)
    if day is None:
        return TemplateBoard(trade_date=None, next_date=None, items=[], total=0)

    rows = db.scalars(
        select(TemplatePool)
        .where(TemplatePool.trade_date == day)
        # 默认按量比降序：这套口径的第一道闸门是「有量」，
        # 量最足的那几只排在前面最符合「明天盯什么」的用法
        .order_by(TemplatePool.vol_ratio.desc())
    ).all()
    if not rows:
        return TemplateBoard(trade_date=day, next_date=None, items=[], total=0)

    next_day, bars = _next_bars(db, day)
    items: list[TemplateItem] = []
    for row in rows:
        floor = round(row.high * TEMPLATE_FLOOR_RATIO, 2) if row.high else None
        bar = bars.get(row.code)
        items.append(
            TemplateItem(
                code=row.code,
                name=row.name,
                trade_date=row.trade_date,
                trigger=row.high,
                floor=floor,
                close=row.close,
                pct_chg=row.pct_chg,
                surge=row.surge,
                vol_ratio=row.vol_ratio,
                close_pos=row.close_pos,
                amount=row.amount,
                next_result=_next_result(row, bar, floor),
                next_pct_chg=bar.pct_chg if bar is not None else None,
            )
        )
    return TemplateBoard(trade_date=day, next_date=next_day, items=items, total=len(items))
