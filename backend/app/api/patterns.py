"""形态选股的查询接口。

**一次查全、前端筛**：命中量每天几百条，`/hits` 的 SQL 一次把当天全部取出来，
前端自己按形态与分数切 —— 沿用板块页 `/ranking` 的做法（那里一次返回 465 个板块
给前端反复切排序）。拖滑块就发一次请求是没必要的，而且筛选逻辑放前端，
「标签上的家数」和「筛出来的行数」天然不会打架。

⚠️ 但**响应**会按 `limit` 截断（默认 50，2026-09-24 用户要求从 500 改小）。
所以「家数」要看 `/summary` 的全量统计，别拿返回条数当总数 ——
前端在截断时会给提示（`HIT_LIMIT`）。
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import PatternHit, StockUniverse
from app.schemas import (
    PatternCount,
    PatternHitItem,
    PatternMeta,
    PatternStockOut,
    PatternSummary,
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
    pattern: str | None = Query(None, description="只看某个形态的全部命中"),
    limit: int = Query(50, ge=1, le=3000),
) -> list[PatternStockOut]:
    """某交易日的命中，**按股票归并**（一只票命中多个形态就是一行多标签）。

    ## 两种模式（2026-09-27 用户要求区分）

    - **不传 `pattern`**：全市场命中按评分降序取前 `limit` 只 —— 列表页的默认视图，
      「今天最值得看的几十只」。
    - **传 `pattern`**：只返回命中该形态的票，且**不再按 50 截断**（调用方传大
      `limit`）—— 「点进一个形态就该看到它的全部命中」，这是用户点名要的。

    ⚠️ 形态模式下**「评分」与排序都换成该形态的分数**：一只票可能同时命中了别的
    更高分的形态，但那一屏讲的只是当前这个形态，拿别的高分来排序会让人对不上账。
    每行仍会带上它命中的**全部**形态（`patterns` 数组，各自带自己的分数），
    所以信息没有丢。

    ⚠️ 取「命中该形态的票」之后要按 code 把当天的行**全部**查回来，不能直接按
    `pattern` 过滤行 —— 那样每只票的标签就只剩选中的那一个了。
    """
    target = trade_date or _latest_date(db)
    if target is None:
        return []

    if pattern is not None:
        if pattern not in _META:
            raise HTTPException(
                status_code=400,
                detail=f"未知形态 {pattern}，可选：{', '.join(_META)}",
            )
        codes = list(
            db.scalars(
                select(PatternHit.code)
                .where(
                    PatternHit.trade_date == target,
                    PatternHit.pattern == pattern,
                    PatternHit.score >= min_score,
                )
                .distinct()
            )
        )
        if not codes:
            return []
        rows = db.scalars(
            select(PatternHit)
            .where(
                PatternHit.trade_date == target,
                PatternHit.code.in_(codes),
                # `min_score` 两种模式下都过滤行，口径一致（默认 0 时是空操作）
                PatternHit.score >= min_score,
            )
            .order_by(PatternHit.score.desc())
        ).all()
    else:
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
    if pattern is not None:
        # 形态模式：把「评分」换成**该形态的分数**再排一次。
        # 上面那句 max 取的是这只票的最高形态分；在当前这一屏里那不是它该显的分
        # （页面上每行的其它形态仍各自带自己的分数，见 `patterns` 数组）
        for stock in result:
            for item in stock.patterns:
                if item.pattern == pattern:
                    stock.score = item.score
                    break
        result.sort(key=lambda stock: stock.score, reverse=True)
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
