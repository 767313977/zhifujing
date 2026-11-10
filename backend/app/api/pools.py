"""选股池接口：RPS 相对强度榜、首板一进二候选 + 两者的次日验证。

候选池都是**盘后**口径（本站没有盘中数据），参数全部由 `services.pools` 决定；
响应里把实际使用的筛选条件原样带回去，页面照原样展示，避免「条件悄悄变了」。

## 缓存设计（P0-2 加固）

`/track` 每次要把近 N 天×全市场日线过一遍（20 天约 10 万行），而且页面上的
参数一动就会重算。

缓存键现在分两部分：
1. **条件**：全部筛选参数（与以前相同）
2. **版本向量**：来自 `services.data_revision.status()` —— 每个相关数据域一张表，
   表上有 INSERT / UPDATE / DELETE 触发器，任何写入都会推进 revision 计数器。
   这样同一天里数据修订后旧缓存自动失效，不再依赖「最新交易日」这个粒度太粗的 key。

`data_revision` 不存在或触发器未安装时 → 缓存自动关闭（fail closed），请求直接查库。
这是 P1.3 已有的安全机制，不是本次重新发明。

## single-flight

`_cached()` 释放全局锁后才调 `build()`，所以多请求并发 miss 同一键时会重复计算。
用 `_inflight` dict + `threading.Event` 做同键去重：第一个负责计算，其他等它，
不阻塞不同键的请求。等待设 60 秒超时，防止永久阻塞。
"""

import logging
import threading
from dataclasses import asdict
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import LimitPool, RpsDaily, SectorDaily, SectorMember
from app.schemas import (
    FirstBoardOut,
    FirstBoardRowOut,
    FirstBoardTrackDayOut,
    FirstBoardTrackOut,
    IndustryCountOut,
    RpsOut,
    RpsRowOut,
    RpsTrackDayOut,
    RpsTrackOut,
    RpsTrackStatOut,
    SectorMembersOut,
    SectorMemberRowOut,
    SectorRpsOut,
    SectorRpsRowOut,
    SectorStockOut,
    SectorStockRowOut,
    SectorStockSectorOut,
    SectorStockTrackDayOut,
    SectorStockTrackOut,
    SectorStockTrackStatOut,
    SectorTrackDayOut,
    SectorTrackOut,
    SectorTrackStatOut,
)
from app.services import data_revision, pools, sector_rps as sector_rps_service
from app.services.stock_meta import industries_of, sectors_of

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pools", tags=["选股池"])

#: 验证结果的缓存时间。数据只会在收盘采集后变，10 分钟足够挡住「拖参数」的重复计算
_TRACK_TTL = timedelta(minutes=10)
_TRACK_CACHE_MAX = 128
#: follower 等待 leader 的上限（秒）。超时后**不自行重算**，返回 503 交调用方重试，
#: 否则被合并的同一请求会在超时瞬间重新分裂成 N 份昂贵计算。
_SINGLE_FLIGHT_TIMEOUT = 60
#: 503 响应里的建议重试间隔（秒）
_RETRY_AFTER_SECONDS = 2

_track_cache: dict[tuple, tuple[datetime, object]] = {}
_track_lock = threading.Lock()
#: single-flight：同一键并发请求的 Event（key → Event）。首个计算完 set，等待者醒。
_inflight: dict[tuple, threading.Event] = {}
_inflight_lock = threading.Lock()

#: 缓存未命中的哨兵（缓存值本身可能是 None，不能用 None 表示 miss）
_MISS = object()


def _effective_key(raw_key: tuple, session: Session) -> tuple | None:
    """把 `data_revision` 版本向量拼进 `raw_key`，返回最终缓存键。

    revision 读不到 → 返回 None（跳过缓存）。
    """
    state = data_revision.status(session)
    if not state.ready:
        data_revision.log_unavailable(state)
        return None
    # version 是 tuple((domain, revision), ...)，identity 是 ("db", (path, dev, ino), uuid)
    return ("rev", state.identity, state.version) + raw_key


def _read_cache(final_key: tuple):
    """TTL 内命中返回缓存值，否则返回 `_MISS`。"""
    with _track_lock:
        hit = _track_cache.get(final_key)
        if hit is None:
            return _MISS
        stamp, value = hit
        if datetime.now() - stamp >= _TRACK_TTL:
            return _MISS
        return value


def _store_cache(final_key: tuple, value) -> None:
    """写入缓存并做容量淘汰。"""
    with _track_lock:
        _track_cache[final_key] = (datetime.now(), value)
        if len(_track_cache) > _TRACK_CACHE_MAX:
            oldest = min(_track_cache, key=lambda item: _track_cache[item][0])
            _track_cache.pop(oldest, None)


def _register_or_join(final_key: tuple) -> tuple[threading.Event, bool]:
    """抢占同键 leader；已有 leader 则返回它的 Event。

    返回 `(event, is_leader)`。`is_leader=True` 时调用方负责计算与清理。
    """
    event = threading.Event()
    with _inflight_lock:
        existing = _inflight.get(final_key)
        if existing is None:
            _inflight[final_key] = event
            return event, True
        return existing, False


def _busy() -> HTTPException:
    """繁忙响应：不暴露内部异常，只给可重试提示。"""
    return HTTPException(
        status_code=503,
        detail="选股结果正在计算中，请稍后重试",
        headers={"Retry-After": str(_RETRY_AFTER_SECONDS)},
    )


def _run_leader(final_key: tuple, event: threading.Event, build):
    """执行一次昂贵计算，并保证 inflight 清理与等待者唤醒。

    成功 → 先写缓存再清 inflight，保证醒来的 follower 一定命中缓存；
    失败 → 不写缓存，异常原样上抛（不吞），但 inflight 仍会被清、event 仍会 set。
    """
    try:
        value = build()
    except Exception:
        logger.exception("single-flight leader 计算失败，key=%s", final_key[:4])
        raise
    else:
        _store_cache(final_key, value)
        return value
    finally:
        with _inflight_lock:
            _inflight.pop(final_key, None)
        event.set()


def _cached(raw_key: tuple, build, session: Session):
    """带 single-flight + data_revision 版本向量的缓存。

    安全约定：
    - revision 不可用 → 直查、不读不写缓存（fail closed）
    - 同一键同一时刻只有 leader 计算；follower 只等待，不重复计算
    - follower 等待超时 → 复查缓存；仍未命中且 leader 仍在算 → 抛 503（可重试），
      **绝不自行 build**，避免超时瞬间把合并的请求重新放大成 N 份计算
    - follower 醒来但缓存未命中且 inflight 已空（leader 异常退出）→ 仅有一个
      follower 能重新抢到 leader 补算，其余仍返回 503，不会形成风暴
    - revision / db_uuid 变化 → 键不同，旧计算不会污染新键
    """
    final_key = _effective_key(raw_key, session)

    # ---- revision 不可用：直查 ----
    if final_key is None:
        return build()

    # ---- 先查缓存 ----
    hit = _read_cache(final_key)
    if hit is not _MISS:
        return hit

    # ---- single-flight：同键去重 ----
    event, is_leader = _register_or_join(final_key)
    if is_leader:
        return _run_leader(final_key, event, build)

    # ---- follower：只等待，不计算 ----
    event.wait(_SINGLE_FLIGHT_TIMEOUT)

    # leader 成功时缓存必已就绪
    hit = _read_cache(final_key)
    if hit is not _MISS:
        return hit

    # 缓存未命中：判断 leader 是否仍在计算
    with _inflight_lock:
        still_running = final_key in _inflight

    if still_running:
        logger.warning(
            "single-flight 等待 %ss 未完成，返回 503（不重算），key=%s",
            _SINGLE_FLIGHT_TIMEOUT, final_key[:4],
        )
        raise _busy()

    # leader 异常退出：允许一个 follower 补算，其余继续 503，避免风暴
    logger.info("leader 已退出但缓存为空，尝试补算，key=%s", final_key[:4])
    retry_event, retry_is_leader = _register_or_join(final_key)
    if retry_is_leader:
        return _run_leader(final_key, retry_event, build)
    raise _busy()


@router.get("/rps", response_model=RpsOut)
def rps(
    trade_date: date | None = Query(None, alias="date", description="缺省 = 库里最新 RPS 日"),
    min_rps: float = Query(0, ge=0, le=100, description="`mode=all` 时两档分位都要 ≥ 这个值"),
    limit: int = Query(100, ge=1, le=500),
    mode: str = Query("all", pattern="^(dual|line_red|all)$", description="档位：全部 / 双高 / 一线红"),
    breakout_only: bool = Query(False, description="只留当天命中突破类形态的票"),
    trend_template: bool = Query(False, description="只留满足 Minervini 趋势模板的票"),
    order: str = Query(
        "strength",
        pattern="^(strength|factor)$",
        description="排序：strength = 按档位原口径；factor = 在这批里按量价因子分重排",
    ),
    session: Session = Depends(get_db),
) -> RpsOut:
    """RPS 榜（默认按 RPS250 排；`dual` 只在近一年有效，见函数说明）。"""
    latest = session.scalar(select(func.max(RpsDaily.trade_date)))
    if latest is None:
        raise HTTPException(status_code=404, detail="还没有 RPS 数据（等收盘扫描跑一轮）")
    # 全池 + 因子分 / 趋势模板时单次 1~6 秒，页面每次加载都会请求 → 与 track 一样缓存，
    # 键带上全部条件参数与库里最新 RPS 日，换参数或数据更新自然失效
    key = (
        "rps_board",
        latest,
        trade_date,
        min_rps,
        limit,
        mode,
        breakout_only,
        trend_template,
        order,
    )
    board = _cached(
        key,
        lambda: pools.rps_board(
            session,
            trade_date=trade_date,
            min_rps=min_rps,
            limit=limit,
            mode=mode,
            breakout_only=breakout_only,
            trend_template=trend_template,
            order=order,
        ),
        session=session,
    )
    if board.trade_date is None:
        raise HTTPException(status_code=404, detail="还没有 RPS 数据（等收盘扫描跑一轮）")
    return RpsOut(
        trade_date=board.trade_date,
        rows=[RpsRowOut(**asdict(row)) for row in board.rows],
        industry_top=[
            IndustryCountOut(industry=name, count=count)
            for name, count in board.industry_top
        ],
        selection_scope=board.selection_scope,
        selection_cap=board.selection_cap,
        evaluated_count=board.evaluated_count,
    )


@router.get("/first-board", response_model=FirstBoardOut)
def first_board(
    trade_date: date | None = Query(None, alias="date"),
    before: str = Query("140000", pattern=r"^\d{6}$", description="首封时间早于它（HHMMSS）"),
    min_float_mv: float = Query(20, ge=0, description="流通市值下限（亿）"),
    max_float_mv: float = Query(300, ge=0, description="流通市值上限（亿）"),
    min_amount: float = Query(3, ge=0, description="成交额下限（亿）"),
    min_themes: int = Query(3, ge=0, le=50, description="同题材涨停家数下限"),
    min_seal_ratio: float = Query(0, ge=0, le=100, description="封单额/流通市值 下限（%）"),
    only_true_first_board: bool = Query(False, description="只看真首板（近 10 日没涨停过）"),
    order: str = Query(
        "board",
        pattern="^(board|factor)$",
        description="排序：board = 题材/封单/首封综合分；factor = 量价因子分（不改变入选集合）",
    ),
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_db),
) -> FirstBoardOut:
    """首板一进二候选（盘后）。"""
    if min_float_mv > max_float_mv:
        raise HTTPException(status_code=400, detail="流通市值下限不能大于上限")
    board = pools.first_board(
        session,
        trade_date=trade_date,
        before=before,
        min_float_mv=min_float_mv,
        max_float_mv=max_float_mv,
        min_amount=min_amount,
        min_themes=min_themes,
        min_seal_ratio=min_seal_ratio,
        only_true_first_board=only_true_first_board,
        order=order,
        limit=limit,
    )
    if board.trade_date is None:
        raise HTTPException(status_code=404, detail="还没有涨停池数据")
    return FirstBoardOut(
        trade_date=board.trade_date,
        conditions=board.conditions,
        rows=[FirstBoardRowOut(**asdict(row)) for row in board.rows],
    )


@router.get("/first-board/track", response_model=FirstBoardTrackOut)
def first_board_track(
    before: str = Query("140000", pattern=r"^\d{6}$", description="首封时间早于它（HHMMSS）"),
    min_float_mv: float = Query(20, ge=0, description="流通市值下限（亿）"),
    max_float_mv: float = Query(300, ge=0, description="流通市值上限（亿）"),
    min_amount: float = Query(3, ge=0, description="成交额下限（亿）"),
    min_themes: int = Query(3, ge=0, le=50, description="同题材涨停家数下限"),
    min_seal_ratio: float = Query(0, ge=0, le=100, description="封单额/流通市值 下限（%）"),
    only_true_first_board: bool = Query(False, description="只看真首板（近 10 日没涨停过）"),
    days: int = Query(20, ge=1, le=60, description="回看多少个有涨停池的交易日"),
    order: str = Query(
        "board",
        pattern="^(board|factor)$",
        description="排序：board = 题材/封单/首封综合分；factor = 量价因子分",
    ),
    top: int = Query(
        0,
        ge=0,
        le=500,
        description="只统计排序后的前 N 只；0 = 全部候选（全量时顺序不影响汇总）",
    ),
    session: Session = Depends(get_db),
) -> FirstBoardTrackOut:
    """近 `days` 天按当前条件选出的首板，次日的晋级 / 开盘溢价 / 收盘收益。

    参数与 `/first-board` 完全一致 —— 改这里的参数看汇总变化，就是定阈值的办法。
    `top>0` 时才会只统计前 N 只，`order` 也才有意义。
    """
    if min_float_mv > max_float_mv:
        raise HTTPException(status_code=400, detail="流通市值下限不能大于上限")
    latest = session.scalar(select(func.max(LimitPool.trade_date)))
    if latest is None:
        raise HTTPException(status_code=404, detail="还没有涨停池数据")
    key = (
        "first_board",
        latest,
        days,
        before,
        min_float_mv,
        max_float_mv,
        min_amount,
        min_themes,
        min_seal_ratio,
        only_true_first_board,
        order,
        top,
    )
    track = _cached(
        key,
        lambda: pools.first_board_track(
            session,
            days=days,
            before=before,
            min_float_mv=min_float_mv,
            max_float_mv=max_float_mv,
            min_amount=min_amount,
            min_themes=min_themes,
            min_seal_ratio=min_seal_ratio,
            only_true_first_board=only_true_first_board,
            order=order,
            top=top,
        ),
        session=session,
    )
    return FirstBoardTrackOut(
        trade_date=track.trade_date,
        conditions=track.conditions,
        days=[FirstBoardTrackDayOut(**asdict(row)) for row in track.days],
        **track.summary,
    )


@router.get("/sector-rps", response_model=SectorRpsOut)
def sector_rps(
    trade_date: date | None = Query(None, alias="date", description="缺省 = 库里最新板块日"),
    taxonomy: str = Query(
        "kph_selected",
        pattern="^(kph_selected|kph_industry)$",
        description="精选板块 / 行业，两个口径不可混比",
    ),
    limit: int = Query(30, ge=1, le=200),
    session: Session = Depends(get_db),
) -> SectorRpsOut:
    """板块强度榜：按 20 日板块涨幅的横截面分位降序。

    数据源是 `sector_daily.pct_chg`（板块指数当日涨跌），**不自己拼成分** ——
    `sector_member` 只有当天快照，拿它回看历史会有前视偏差。
    """
    board_result = sector_rps_service.board(
        session, trade_date=trade_date, taxonomy=taxonomy, limit=limit
    )
    if board_result.trade_date is None:
        raise HTTPException(status_code=404, detail="还没有板块日线数据")
    return SectorRpsOut(
        trade_date=board_result.trade_date,
        taxonomy=board_result.taxonomy,
        rows=[SectorRpsRowOut(**asdict(row)) for row in board_result.rows],
    )


@router.get("/sector-rps/members", response_model=SectorMembersOut)
def sector_rps_members(
    code: str = Query(..., description="板块代码，取自 `/sector-rps` 每行的 `code`"),
    trade_date: date | None = Query(None, alias="date", description="缺省 = 该板块最近一次快照日"),
    session: Session = Depends(get_db),
) -> SectorMembersOut:
    """板块成分股：点开 RPS 榜里的板块名时看「这个板块里都有谁」。

    ⚠️ 数据是 `sector_member` 的**快照**（22:00 预取 + 打开时现取），没有历史回补：
    点一个旧日期可能根本没有快照。那种情况回落到该板块最近一次快照，并把实际日期
    回给页面 —— 给一张空表会让人以为「板块是空的」。
    """

    def _rows(day: date) -> list[SectorMember]:
        return list(
            session.scalars(
                select(SectorMember)
                .where(
                    SectorMember.sector_code == code,
                    SectorMember.trade_date == day,
                )
                .order_by(SectorMember.pct_chg.desc().nulls_last(), SectorMember.code)
            )
        )

    latest = session.scalar(
        select(func.max(SectorMember.trade_date)).where(SectorMember.sector_code == code)
    )
    if latest is None:
        return SectorMembersOut(trade_date=None, sector_code=code, rows=[])

    day = trade_date or latest
    rows = _rows(day)
    if not rows and trade_date is not None:
        day, rows = latest, _rows(latest)

    # 板块 / 行业与形态页同源（`stock_meta`）：成分表要回答「这只票是什么、做过什么」。
    # 332 只的板块一次查完（内部按 900 分批），不会变成每只一条查询。
    codes = {row.code for row in rows}
    sectors = sectors_of(session, codes)
    industries = industries_of(session, codes)

    return SectorMembersOut(
        trade_date=day,
        sector_code=code,
        rows=[
            SectorMemberRowOut(
                code=row.code,
                name=row.name,
                close=row.close,
                pct_chg=row.pct_chg,
                amount=row.amount,
                turnover=row.turnover,
                sectors=sectors.get(row.code, []),
                industry=industries.get(row.code),
            )
            for row in rows
        ],
    )


@router.get("/sector-rps/track", response_model=SectorTrackOut)
def sector_rps_track(
    top: int = Query(5, ge=1, le=30, description="每天取 RPS 最高的几个板块"),
    days: int = Query(20, ge=5, le=60, description="回看多少个有板块数据的交易日"),
    taxonomy: str = Query(
        "kph_selected",
        pattern="^(kph_selected|kph_industry)$",
        description="精选板块 / 行业",
    ),
    session: Session = Depends(get_db),
) -> SectorTrackOut:
    """板块动量的延续性：最强 `top` 个板块在之后 5 / 20 日的表现与超额。

    ⚠️ 收益是**板块指数**的收益（成分当日生效），不是个股；不能当成「买板块里的票
    能赚这么多」。
    """
    latest = session.scalar(
        select(func.max(SectorDaily.trade_date)).where(SectorDaily.taxonomy == taxonomy)
    )
    if latest is None:
        raise HTTPException(status_code=404, detail="还没有板块日线数据")
    key = ("sector", latest, days, top, taxonomy)
    track = _cached(
        key,
        lambda: sector_rps_service.track(session, days=days, top=top, taxonomy=taxonomy),
        session=session,
    )
    return SectorTrackOut(
        trade_date=track.trade_date,
        days=track.summary["days"],
        verified_days=track.summary["verified_days"],
        top=track.summary["top"],
        ret5=SectorTrackStatOut(**track.summary["ret5"]),
        ret20=SectorTrackStatOut(**track.summary["ret20"]),
        rows=[SectorTrackDayOut(**asdict(row)) for row in track.days],
    )


@router.get("/sector-stock", response_model=SectorStockOut)
def sector_stock(
    trade_date: date | None = Query(None, alias="date", description="缺省 = 库里最新板块日"),
    taxonomy: str = Query(
        "kph_selected",
        pattern="^(kph_selected|kph_industry)$",
        description="精选板块 / 行业，两个口径不可混比",
    ),
    scope: str = Query(
        "strong",
        pattern="^(strong|weak)$",
        description="强势板块（RPS20≥90）/ 弱势板块（RPS20≤10），后者的作用是对照",
    ),
    order: str = Query(
        "gain",
        pattern="^(gain|factor)$",
        description="个股排序：gain = 当日涨幅；factor = 量价因子分",
    ),
    sectors: int = Query(3, ge=1, le=10, description="取几个板块"),
    per_sector: int = Query(3, ge=1, le=10, description="每个板块取几只"),
    session: Session = Depends(get_db),
) -> SectorStockOut:
    """板块强度 → 个股候选（研究视图）。

    ⚠️ **历史超额为负**：IC −0.046、强势板块选股 −0.17%、强势板块 × 突破 −0.30%
    （全市场突破 +0.18%），已剔除 ST 与北交所。见 `docs/回测_板块到个股_20261025.md`。
    上线它是为了让人看见「板块强 ≠ 个股强」，不是买入推荐；页面必须把负超额如实显示。
    """
    board = pools.sector_stock(
        session,
        trade_date=trade_date,
        taxonomy=taxonomy,
        scope=scope,
        order=order,
        sectors=sectors,
        per_sector=per_sector,
        limit=sectors * per_sector,
    )
    if board.trade_date is None:
        raise HTTPException(status_code=404, detail="还没有板块日线数据")
    return SectorStockOut(
        trade_date=board.trade_date,
        conditions=board.conditions,
        sectors=[SectorStockSectorOut(**asdict(row)) for row in board.sectors],
        rows=[SectorStockRowOut(**asdict(row)) for row in board.rows],
    )


@router.get("/sector-stock/track", response_model=SectorStockTrackOut)
def sector_stock_track(
    days: int = Query(20, ge=5, le=60, description="回看多少个有成分股的交易日"),
    taxonomy: str = Query(
        "kph_selected",
        pattern="^(kph_selected|kph_industry)$",
        description="精选板块 / 行业",
    ),
    scope: str = Query("strong", pattern="^(strong|weak)$"),
    order: str = Query("gain", pattern="^(gain|factor)$"),
    sectors: int = Query(3, ge=1, le=10),
    per_sector: int = Query(3, ge=1, le=10),
    session: Session = Depends(get_db),
) -> SectorStockTrackOut:
    """近 `days` 日「板块 → 个股」候选的次日表现（与形态跟踪同一份等权基准）。"""
    latest = session.scalar(select(func.max(SectorMember.trade_date)))
    if latest is None:
        raise HTTPException(
            status_code=404,
            detail="还没有板块成分股数据（等 22:00 预取，或跑 scripts/backfill_sector_members.py）",
        )
    key = ("sector_stock", latest, days, taxonomy, scope, order, sectors, per_sector)
    track = _cached(
        key,
        lambda: pools.sector_stock_track(
            session,
            days=days,
            taxonomy=taxonomy,
            scope=scope,
            order=order,
            sectors=sectors,
            per_sector=per_sector,
        ),
        session=session,
    )
    return SectorStockTrackOut(
        trade_date=track.trade_date,
        conditions=track.conditions,
        verified_days=track.summary["verified_days"],
        samples=track.summary["samples"],
        ret1=SectorStockTrackStatOut(**track.summary["ret1"]),
        rows=[SectorStockTrackDayOut(**asdict(row)) for row in track.days],
    )


@router.get("/rps/track", response_model=RpsTrackOut)
def rps_track(
    min_rps: float = Query(0, ge=0, le=100, description="`mode=all` 时两档分位都要 ≥ 这个值"),
    top: int = Query(50, ge=1, le=200, description="每天取榜单前多少只"),
    days: int = Query(20, ge=1, le=60, description="回看多少个有 RPS 记录的交易日"),
    mode: str = Query("all", pattern="^(dual|line_red|all)$", description="档位：全部 / 双高 / 一线红"),
    breakout_only: bool = Query(False, description="只留当天命中突破类形态的票"),
    trend_template: bool = Query(False, description="只留满足 Minervini 趋势模板的票"),
    order: str = Query(
        "strength",
        pattern="^(strength|factor)$",
        description="排序：strength = 档位原口径；factor = 按量价因子分重排（会改变取前 N 只的结果）",
    ),
    session: Session = Depends(get_db),
) -> RpsTrackOut:
    """RPS 榜前 `top` 只的 1 / 5 日表现与超额（只统计真跑过 RPS 的日子）。"""
    latest = session.scalar(select(func.max(RpsDaily.trade_date)))
    if latest is None:
        raise HTTPException(status_code=404, detail="还没有 RPS 数据（等收盘扫描跑一轮）")
    key = (
        "rps",
        latest,
        days,
        min_rps,
        top,
        mode,
        breakout_only,
        trend_template,
        order,
    )
    track = _cached(
        key,
        lambda: pools.rps_track(
            session,
            days,
            min_rps,
            top,
            mode=mode,
            breakout_only=breakout_only,
            trend_template=trend_template,
            order=order,
        ),
        session=session,
    )
    return RpsTrackOut(
        trade_date=track.trade_date,
        days=track.summary.get("days", 0),
        verified_days=track.summary.get("verified_days", 0),
        samples=track.summary.get("samples", 0),
        ret1=RpsTrackStatOut(**track.summary["ret1"]),
        ret5=RpsTrackStatOut(**track.summary["ret5"]),
        rows=[RpsTrackDayOut(**asdict(row)) for row in track.days],
    )
