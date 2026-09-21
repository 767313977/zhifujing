"""个股详情接口。

数据来自本地缓存的日线（`stock_daily`）。库里**大部分票**是每日由
`jobs/collect_kline.py` 按 `stock_universe`（流动性 / 市值筛过的池子，3032 只）
批量落库的；但**池外的票没有**，所以首次打开某只池外个股时前端会自动调一次
同步，之后走缓存（见设计文档 8.16、8.22.2）。

「所属题材」不在这里现取：它读的是 `stock_concept`（涨停天梯的日度快照），
只覆盖涨停过的票，见 `themes()` 的说明。
"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.jobs.collect_daily import (
    STOCK_FULL_DAYS,
    CollectionBusy,
    DailyCollector,
    collect_guard,
)
from app.models import (
    Lhb,
    LimitPool,
    SectorDaily,
    StockBasic,
    StockConcept,
    StockDaily,
    Watchlist,
)
from app.schemas import StockDailyRow, StockProfile, StockThemeItem, StockThemes
from app.services.patterns import build_bars
from app.sources.ifind import IfindError, normalize_code
from app.sources.kaipanhong import TAXONOMY_SELECTED

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stock", tags=["个股"])

# 可选的 K 线周期。周/月由本地日线重采样（`_resample`），不额外取数
PERIODS = ("day", "week", "month")


def _code(raw: str) -> str:
    code = normalize_code(raw)
    if len(code) != 6:
        raise HTTPException(
            status_code=400, detail=f"股票代码应为 6 位数字，收到「{raw}」"
        )
    return code


def _resolve_name(session: Session, code: str) -> str | None:
    """这只股票叫什么。iFinD 的题材问句只认名称，所以必须先有名字。

    按可信度依次从自选股、股票基础信息、本地日线里找。
    """
    item = session.get(Watchlist, code)
    basic = session.get(StockBasic, code)
    latest = session.scalars(
        select(StockDaily.name)
        .where(StockDaily.code == code, StockDaily.name.is_not(None))
        .order_by(StockDaily.trade_date.desc())
        .limit(1)
    ).first()
    for candidate in (
        item.name if item else None,
        basic.name if basic else None,
        latest,
    ):
        if candidate:
            return candidate
    return None


@router.get("/{code}", response_model=StockProfile)
def profile(code: str, session: Session = Depends(get_db)) -> StockProfile:
    code = _code(code)
    item = session.get(Watchlist, code)
    latest = session.scalars(
        select(StockDaily)
        .where(StockDaily.code == code)
        .order_by(StockDaily.trade_date.desc())
        .limit(1)
    ).first()
    count, first_date, last_date = session.execute(
        select(
            func.count(),
            func.min(StockDaily.trade_date),
            func.max(StockDaily.trade_date),
        ).where(StockDaily.code == code)
    ).one()

    # 把该股与复盘数据关联起来：涨停过哪天、上过几次龙虎榜
    limit_up_dates = list(
        session.scalars(
            select(LimitPool.trade_date)
            .where(LimitPool.code == code, LimitPool.pool_type == "up")
            .order_by(LimitPool.trade_date.desc())
        )
    )
    lhb_count = (
        session.scalar(select(func.count()).select_from(Lhb).where(Lhb.code == code)) or 0
    )

    return StockProfile(
        code=code,
        name=_resolve_name(session, code),
        in_watchlist=item is not None,
        note=item.note if item else None,
        latest=StockDailyRow.model_validate(latest) if latest else None,
        day_count=count or 0,
        first_date=first_date,
        last_date=last_date,
        limit_up_dates=limit_up_dates,
        lhb_count=lhb_count,
    )


@router.get("/{code}/daily", response_model=list[StockDailyRow])
def daily(
    code: str,
    days: int = Query(120, ge=5, le=500, description="返回最近 N 个交易日，按日期升序"),
    adjust: bool = Query(
        False,
        description="是否返回前复权价。形态页要看图确认，而引擎判定用的是复权序列，"
        "不复权图上的除权跳空会让形态标注线画在错误的高度",
    ),
    period: str = Query(
        "day",
        description="day=日K week=周K month=月K。周/月由本地日线重采样得到，不额外取数",
    ),
    session: Session = Depends(get_db),
) -> list[StockDailyRow]:
    target = _code(code)
    if period not in PERIODS:
        raise HTTPException(
            status_code=400,
            detail=f"period 只能是 {'、'.join(PERIODS)}，收到「{period}」",
        )
    rows = list(
        session.scalars(
            select(StockDaily)
            .where(StockDaily.code == target)
            .order_by(StockDaily.trade_date.desc())
            .limit(days)
        )
    )
    rows.reverse()
    if not rows:
        return []

    items = [_daily_item(row) for row in rows]
    if adjust:
        items = _adjusted(items)

    if period != "day":
        items = _resample(items, period)
    return [StockDailyRow(**item) for item in items]


def _daily_item(row: StockDaily) -> dict:
    return {
        "trade_date": row.trade_date,
        "open": row.open,
        "high": row.high,
        "low": row.low,
        "close": row.close,
        "pct_chg": row.pct_chg,
        "volume": row.volume,
        "amount": row.amount,
    }


def _adjusted(items: list[dict]) -> list[dict]:
    """前复权。复用形态引擎的复权逻辑，而不是在前端再实现一遍 ——「按当天收盘价
    做日内换算」这个细节很容易写错，两个实现早晚会不一致。

    OHLC 缺一不可：`sync_stock` 那条路径写进来的行 OHLC 可能为 None，
    `build_bars` 里 `float(None)` 会直接抛 500。缺 OHLC 的行没法做日内换算，跳过。
    """
    usable = [
        item
        for item in items
        if item["close"] is not None
        and item["pct_chg"] is not None
        and item["open"] is not None
        and item["high"] is not None
        and item["low"] is not None
    ]
    if not usable:
        return []
    bars = build_bars(
        [
            {
                "date": item["trade_date"],
                "open": item["open"],
                "high": item["high"],
                "low": item["low"],
                "close": item["close"],
                "volume": item["volume"],
                "amount": item["amount"],
                "pct_chg": item["pct_chg"],
            }
            for item in usable
        ]
    )
    return [
        {
            "trade_date": bars.dates[index],
            "open": float(bars.open[index]),
            "high": float(bars.high[index]),
            "low": float(bars.low[index]),
            "close": float(bars.close[index]),
            # 涨跌幅、成交量、成交额不受复权影响，照原样带过来
            "pct_chg": usable[index]["pct_chg"],
            "volume": usable[index]["volume"],
            "amount": usable[index]["amount"],
        }
        for index in range(len(bars))
    ]


def _resample(items: list[dict], period: str) -> list[dict]:
    """把逐日行情合并成周 K / 月 K。

    分组：周用 ISO 周（`isocalendar()[:2]`，跨年也不会把相邻两天算成同一周），
    月用「年 + 月」。每根的 `trade_date` 取该组**最后一个交易日** —— 图上这根柱子
    标在周末，与常见软件的画法一致。

    周/月的涨跌幅库里没有，只能用相邻两根的收盘价算；第一根没有前一根，记 None
    （成交量柱会退回按「收 - 开」染色）。成交量 / 成交额求和，组内全空就给 None
    （缺数据与 0 是两回事）。
    """
    grouped: list[tuple[tuple, list[dict]]] = []
    for item in items:
        day = item["trade_date"]
        key = (
            day.isocalendar()[:2] if period == "week" else (day.year, day.month)
        )
        if grouped and grouped[-1][0] == key:
            grouped[-1][1].append(item)
        else:
            grouped.append((key, [item]))

    result: list[dict] = []
    previous_close: float | None = None
    for _, group in grouped:
        closes = [row["close"] for row in group if row["close"] is not None]
        opens = [row["open"] for row in group if row["open"] is not None]
        highs = [row["high"] for row in group if row["high"] is not None]
        lows = [row["low"] for row in group if row["low"] is not None]
        volumes = [row["volume"] for row in group if row["volume"] is not None]
        amounts = [row["amount"] for row in group if row["amount"] is not None]
        close = closes[-1] if closes else None
        result.append(
            {
                "trade_date": group[-1]["trade_date"],
                "open": opens[0] if opens else None,
                "high": max(highs) if highs else None,
                "low": min(lows) if lows else None,
                "close": close,
                "pct_chg": (
                    None
                    if close is None or not previous_close
                    else (close / previous_close - 1) * 100
                ),
                "volume": sum(volumes) if volumes else None,
                "amount": sum(amounts) if amounts else None,
            }
        )
        previous_close = close
    return result


@router.get("/{code}/themes", response_model=StockThemes)
def themes(code: str, session: Session = Depends(get_db)) -> StockThemes:
    """该股**最近一次涨停时**所属的开盘红精选板块，附带板块最近一个交易日的涨跌幅。

    数据来自 `stock_concept`（涨停天梯的日度快照），这里取该股出现过的最近一天 ——
    所以它回答的是「这只票最近一次涨停是因为哪个板块」，**不是**「它属于哪些概念」。
    这个语义差别要记住：开盘红没有非涨停个股的板块归属接口（见设计文档 8.32.4），
    对从未涨停过的票这里就是空的，如实返回空列表，不编一个概念出来。
    """
    code = _code(code)
    name = _resolve_name(session, code)

    latest = session.scalar(
        select(func.max(StockConcept.trade_date)).where(StockConcept.code == code)
    )
    concepts: list[str] = []
    if latest is not None:
        concepts = sorted(
            session.scalars(
                select(StockConcept.concept).where(
                    StockConcept.code == code, StockConcept.trade_date == latest
                )
            )
        )

    # 板块表现：取板块表里精选口径的最新交易日，能对上的顺带给板块代码
    board_date = session.scalar(
        select(func.max(SectorDaily.trade_date)).where(
            SectorDaily.taxonomy == TAXONOMY_SELECTED
        )
    )
    boards: dict[str, tuple[str | None, float | None]] = {}
    if board_date is not None:
        boards = {
            row.name: (row.sector_code, row.pct_chg)
            for row in session.scalars(
                select(SectorDaily).where(
                    SectorDaily.trade_date == board_date,
                    SectorDaily.taxonomy == TAXONOMY_SELECTED,
                )
            )
            if row.name
        }

    items = [
        StockThemeItem(
            concept=concept,
            board_code=boards.get(concept, (None, None))[0],
            pct_chg=boards.get(concept, (None, None))[1],
        )
        for concept in concepts
    ]
    # 能对上板块的排前面，其次按板块涨跌幅降序 —— 一眼看到这只票最热的题材
    items.sort(key=lambda item: (item.board_code is None, -(item.pct_chg or 0)))

    return StockThemes(code=code, name=name, board_date=board_date, themes=items)


@router.post("/{code}/sync")
def sync(
    code: str,
    days: int = Query(STOCK_FULL_DAYS, ge=5, le=500, description="回看的交易日数"),
) -> dict:
    """同步该股日线到本地缓存。首次打开个股页时调用。"""
    try:
        with collect_guard("个股同步"):
            written = DailyCollector().sync_stock(_code(code), days=days)
    except CollectionBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IfindError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"rows": written}
