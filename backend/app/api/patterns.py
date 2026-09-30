"""形态选股的查询接口。

**一次查全、前端筛**：命中量每天几百条，`/hits` 的 SQL 一次把当天全部取出来，
前端自己按形态与分数切 —— 沿用板块页 `/ranking` 的做法（那里一次返回 465 个板块
给前端反复切排序）。拖滑块就发一次请求是没必要的，而且筛选逻辑放前端，
「标签上的家数」和「筛出来的行数」天然不会打架。

⚠️ 但**响应**会按 `limit` 截断（默认 50，2026-09-24 用户要求从 500 改小）。
所以「家数」要看 `/summary` 的全量统计，别拿返回条数当总数 ——
前端在截断时会给提示（`HIT_LIMIT`）。
"""

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import PatternHit, StockBasic, StockConcept, StockDaily, StockUniverse
from app.schemas import (
    PatternCount,
    PatternHitItem,
    PatternMeta,
    PatternStockOut,
    PatternSummary,
    PatternTrackDetail,
    PatternTrackOut,
)
from app.services import pattern_track
from app.services.patterns import PATTERNS

router = APIRouter(prefix="/api/patterns", tags=["patterns"])

_META = {pattern.key: pattern for pattern in PATTERNS}
# 注册表里**当前存在**的 key，所有查询都必须带上这道过滤。
#
# 为什么需要它：形态被删掉之后，**历史那几天的 `pattern_hit` 行还在库里**
# （扫描是「先删后插」，但它只重写自己扫的那一天，没人去清旧日期）。少这道过滤
# 会出现三件怪事：① 被删形态的命中混进默认列表，标签名显示成 key、分组显示「其他」；
# ② 「共 N 只命中」把它们的去重只数也算进去，与筛选条上各家数之和对不上；
# ③ 飞书简报的口径跟着一起错。
# 过滤放在查询侧而不是去删库：删库要在云端也执行一遍 SQL，而过滤跟着代码走，
# 一次部署就生效，历史行留着也无害。
_KEYS = tuple(_META)

# `/track` 的结果缓存。它是全站最重的一个读接口（30 个循环 × 30 天要从 `stock_daily`
# 拉 40 多万行压成矩阵，实测 2.4 秒），而**它依赖的数据一天只变一次**（当天采集跑完）。
# 缓存键里带上「数据版本」= (最新命中日, 最新日线日)，所以采集一写库就自动失效，
# 不需要靠 TTL 猜；TTL 只是顺手限制内存（键最多 4×2×2 种组合，本来也不会涨）。
_TRACK_CACHE: dict[tuple, tuple[datetime, PatternTrackOut]] = {}
_TRACK_TTL = timedelta(minutes=10)


def _track_version(db: Session) -> tuple[date | None, date | None]:
    """`/track` 依赖的两张表的最新日期 —— 缓存键的一部分。"""
    return (
        db.scalar(select(func.max(PatternHit.trade_date))),
        db.scalar(select(func.max(StockDaily.trade_date))),
    )


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
                # 被删掉的形态的标签也要滤掉，否则那一行会多出一个「其他」组
                PatternHit.pattern.in_(_KEYS),
                # `min_score` 两种模式下都过滤行，口径一致（默认 0 时是空操作）
                PatternHit.score >= min_score,
            )
            .order_by(PatternHit.score.desc())
        ).all()
    else:
        rows = db.scalars(
            select(PatternHit)
            .where(
                PatternHit.trade_date == target,
                PatternHit.pattern.in_(_KEYS),
                PatternHit.score >= min_score,
            )
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

    # 板块（口径与个股页「所属题材」一致，见 `PatternStockOut.sectors` 的说明）。
    #
    # ⚠️ 取「该股出现过的**最近一天**」而不是命中当天：`stock_concept` 来自涨停天梯、
    # 只有涨停股有值，用当天口径的话这个列表里 97% 的行会是空的。
    # **一次查全、在内存里按 code 挑最新的那天**：写成「每只票一条相关子查询」会把这个
    # 接口从一次查询变成上千次（`code` 不是那张表主键的首列，每次都要扫）。
    concept_rows = db.execute(
        select(StockConcept.code, StockConcept.trade_date, StockConcept.concept).where(
            StockConcept.code.in_({row.code for row in rows})
        )
    ).all()
    latest_seen: dict[str, tuple[date, list[str]]] = {}
    for code, seen_on, concept in concept_rows:
        current = latest_seen.get(code)
        if current is None or seen_on > current[0]:
            latest_seen[code] = (seen_on, [concept])
        elif seen_on == current[0]:
            current[1].append(concept)
    sectors = {code: sorted(names) for code, (_, names) in latest_seen.items()}

    # 同花顺行业（三级路径）。与上面的板块**不是一套分类**，见 `PatternStockOut.industry`。
    # 一次查全即可 —— 建池 7 天一次、覆盖全 A，所以基本没有查不到的行；查不到就是 None。
    industries = dict(
        db.execute(
            select(StockBasic.code, StockBasic.industry).where(
                StockBasic.code.in_({row.code for row in rows})
            )
        ).all()
    )

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
                sectors=sectors.get(row.code, []),
                industry=industries.get(row.code),
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


@router.get("/track", response_model=PatternTrackOut)
def track(
    db: Session = Depends(get_db),
    cohorts: int = Query(pattern_track.DEFAULT_COHORTS, ge=1, le=120),
    top: int = Query(pattern_track.DEFAULT_TOP, ge=1, le=200),
    track_days: int = Query(pattern_track.DEFAULT_TRACK_DAYS, ge=1, le=120),
) -> PatternTrackOut:
    """每个筛选日的「评分前 N 只」在其后 `track_days` 个交易日里的走势（循环跟踪）。

    ## 口径

    - **循环**：最近 `cohorts` 个**有命中记录**的交易日，每个日子就是一个循环
      （不是自然日，也不是日历上的最近 N 个交易日）—— 建站早期只有零星几天有命中，
      所以窗口可能跨得更长，页面会如实写出实际天数。
    - **入选**：每天按票归并取最高分、降序取前 `top` 只 —— 与 `/hits` 默认视图、
      以及每天补 DDE 的那批**同一口径**。
    - **收益**：筛选日收盘 → 之后第 n 个交易日收盘（n = 1..`track_days`），
      **按涨跌幅逐日复利**（即前复权口径，见 `services.pattern_track` 的模块说明）。
    - **胜率**：每个点都给两个 —— 上涨占比（`up_pct`）与跑赢当天全市场平均的比例
      （`beat_pct`），外加平均超额（`excess`）。`average` 是所有循环摊平后的同一条线。
    - **样本**：逐循环不去重（同一只票连上三个循环算三个样本），同时给出去重只数。

    ⚠️ 循环还没走到第 n 个交易日时**不返回那个点**（`progress` 只记到能算的那个 n），
    所以越新的循环 `points` 越短。
    """
    # 缓存键里连「统计起点」一起带上：改了配置就该立刻生效，不能吃旧结果
    start = get_settings().pattern_track_start
    key = (cohorts, top, track_days, start, *_track_version(db))
    cached = _TRACK_CACHE.get(key)
    if cached is not None and datetime.now() - cached[0] < _TRACK_TTL:
        return cached[1]

    result = PatternTrackOut.model_validate(
        pattern_track.track(
            db, start=start, cohorts=cohorts, top=top, track_days=track_days
        )
    )
    _TRACK_CACHE[key] = (datetime.now(), result)
    return result


@router.get("/track/detail", response_model=PatternTrackDetail)
def track_detail(
    db: Session = Depends(get_db),
    trade_date: date = Query(..., alias="date", description="哪一天的循环"),
    top: int = Query(pattern_track.DEFAULT_TOP, ge=1, le=200),
    track_days: int = Query(pattern_track.DEFAULT_TRACK_DAYS, ge=1, le=120),
) -> PatternTrackDetail:
    """某个循环选中的票的**逐日明细**：是哪 50 只、之后每个交易日各涨跌多少。

    - 入选口径与 `/track` 完全一致（按票归并取最高分的前 `top` 只、只认注册表里的形态），
      所以两边的「只数」永远对得上。
    - `pct` 的每一列是**那一天的当日涨跌幅**（相对前一交易日），不是从筛选日起算的累计。
    - `days` 给出每列对应的实际交易日；`progress` = 已经走到第几个交易日（与 `/track` 同义）。
    - 只走到「最后一天真有行情」那天，没走到的日子**不返回**（不是返回 null 让你以为是
      当天没涨跌）。
    """
    return PatternTrackDetail.model_validate(
        pattern_track.detail(db, trade_date, top=top, track_days=track_days)
    )


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
        .where(PatternHit.trade_date == target, PatternHit.pattern.in_(_KEYS))
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
                PatternHit.trade_date == target, PatternHit.pattern.in_(_KEYS)
            )
        )
        or 0,
        by_pattern=by_pattern,
    )
