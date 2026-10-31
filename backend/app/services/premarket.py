"""盘前 / 开盘页的聚合（FEAT-PREMARKET，2026-10-31）。

**只读本地库，不发任何外部请求。** 竞价数据来自 `auction_snapshot`（腾讯批量行情，
交易日 9:15–9:31 采样，见 `jobs/collect_auction.py`）；其余来自昨日涨停池、
`pattern_hit`（明日盯 / 明日预案）、`stock_daily`、自选表。

口径（与页面文案一一对应）：

- 「上一交易日」一律按交易日历取（`services.trading_days.previous_trade_day`），
  不按业务表自己的日期 —— 池子缺一天时不会把 3 天前当成「昨日」；
- 「昨收」优先 `auction_snapshot.pre_close`（与竞价涨幅同源），缺失回落上一交易日
  `stock_daily.close`；
- **竞价涨幅 = 今开涨幅**：A 股开盘价就是集合竞价成交价，两个数恒等。所以这里只返回
  `auction_pct` 一个字段，另给 `post_open_pct`（9:31 现价 ÷ 昨收 − 1，看高开之后
  站没站住）—— 与 `schemas.AuctionRow` 不重复造「开盘涨幅」的决定一致；
- 竞价没采到**不是错误**：`auction_available=False`，各行竞价相关字段为 null，
  页面显示「今日竞价未采到」；非交易日同样照常返回（`is_trade_day=False`）。

「相对关键价」的取法沿用悟道页现有的 `key_levels`：
- 明天盯（`wudao_sample`）：`watch_high` = **信号日（= 上一交易日）最高价**；
- 明天预案（`wudao_start`）：`breakout` = **样板日最高价**（信号日的前一天）。

止损/买入价之类的价位**不在这里算**：盘前页只回答「今天开在哪、离关键价多远」。
"""

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AuctionSnapshot,
    CbBasic,
    EtfBasic,
    LimitPool,
    PatternHit,
    StockBasic,
    StockDaily,
    Watchlist,
)
from app.services.trading_days import is_trade_day, previous_trade_day
from app.services.watch_scope import latest_quotes
from app.timeutil import today_cn

#: 悟道两个盘前名单的 key 与显示名（注册表在 `services.patterns.PATTERNS`）
WUDAO_KEYS = {"wudao_sample": "明天盯", "wudao_start": "明天预案"}

#: 高开 / 低开的判定阈值（%）。平开区间刻意留得窄：盘前看的都是短线，
#: ±0.5% 以内分不出方向，标「平开」比强行给「高开」更诚实。
FLAT_BAND = 0.5


def _auction_meta(session: Session, day: date) -> dict[str, Any]:
    """当日竞价的「有没有、多新、覆盖多少」。"""
    count = (
        session.scalar(
            select(func.count())
            .select_from(AuctionSnapshot)
            .where(AuctionSnapshot.trade_date == day)
        )
        or 0
    )
    result_count = (
        session.scalar(
            select(func.count())
            .select_from(AuctionSnapshot)
            .where(
                AuctionSnapshot.trade_date == day,
                AuctionSnapshot.price.is_not(None),
            )
        )
        or 0
    )
    updated_at = session.scalar(
        select(func.max(AuctionSnapshot.updated_at)).where(
            AuctionSnapshot.trade_date == day
        )
    )
    return {
        "auction_available": result_count > 0,
        "auction_count": int(count),
        "auction_result_count": int(result_count),
        "auction_updated_at": updated_at,
    }


def _auction_map(session: Session, day: date, codes: list[str]) -> dict[str, AuctionSnapshot]:
    if not codes:
        return {}
    return {
        row.code: row
        for row in session.scalars(
            select(AuctionSnapshot).where(
                AuctionSnapshot.trade_date == day,
                AuctionSnapshot.code.in_(codes),
            )
        )
    }


def _daily_map(session: Session, day: date, codes: list[str]) -> dict[str, StockDaily]:
    """指定交易日的日线（用来拿昨收 / 昨高）。"""
    if not codes or day is None:
        return {}
    return {
        row.code: row
        for row in session.scalars(
            select(StockDaily).where(
                StockDaily.trade_date == day,
                StockDaily.code.in_(codes),
            )
        )
    }


def _open_status(auction: AuctionSnapshot | None) -> str | None:
    """开盘状态标签。**只在有竞价数据时给**，缺数据一律 null（页面显示「—」）。"""
    if auction is None or auction.pct_chg is None:
        return None
    price, limit_up, limit_down = auction.price, auction.limit_up, auction.limit_down
    if price is not None and limit_up is not None and limit_up > 0 and price >= limit_up:
        return "一字"
    if price is not None and limit_down is not None and limit_down > 0 and price <= limit_down:
        return "一字跌停"
    if auction.pct_chg >= FLAT_BAND:
        return "高开"
    if auction.pct_chg <= -FLAT_BAND:
        return "低开"
    return "平开"


def _open_pct(auction: AuctionSnapshot | None) -> float | None:
    """9:31 现价相对昨收的涨幅（%）。竞价涨幅与开盘涨幅恒等，不重复给。"""
    if auction is None or auction.post_open_price is None or not auction.pre_close:
        return None
    return round((auction.post_open_price / auction.pre_close - 1.0) * 100.0, 2)


def _limit_rows(session: Session, prev: date | None, day: date) -> list[dict[str, Any]]:
    """昨日涨停 / 炸板 + 今日竞价表现。

    `pool_type` 取 up（涨停）与 broken（炸板）：炸板票的次日开盘同样是盘前要看的
    「情绪是否修复」证据。跌停池不放进来 —— 与「涨停今开」不是同一个问题。
    """
    if prev is None:
        return []
    pools = list(
        session.scalars(
            select(LimitPool).where(
                LimitPool.trade_date == prev,
                LimitPool.pool_type.in_(("up", "broken")),
            )
        )
    )
    if not pools:
        return []
    codes = [row.code for row in pools]
    auctions = _auction_map(session, day, codes)
    dailies = _daily_map(session, prev, codes)

    rows: list[dict[str, Any]] = []
    for pool in pools:
        auction = auctions.get(pool.code)
        daily = dailies.get(pool.code)
        prev_close = (
            auction.pre_close
            if auction is not None and auction.pre_close
            else (daily.close if daily is not None else None)
        )
        rows.append(
            {
                "code": pool.code,
                "name": pool.name or (auction.name if auction else None),
                "pool_type": "涨停" if pool.pool_type == "up" else "炸板",
                "consecutive": pool.consecutive,
                "prev_close": prev_close,
                "prev_high": daily.high if daily is not None else None,
                "auction_price": auction.price if auction else None,
                "auction_pct": auction.pct_chg if auction else None,
                "post_open_pct": _open_pct(auction),
                "status": _open_status(auction),
            }
        )
    # 连板高度优先、其次竞价涨幅 —— 盘前先看「最高板怎么开」
    rows.sort(
        key=lambda row: (
            -(row["consecutive"] or 0),
            -(row["auction_pct"] if row["auction_pct"] is not None else -999),
            row["code"],
        )
    )
    return rows


def _wudao_rows(session: Session, prev: date | None, day: date) -> list[dict[str, Any]]:
    """上一交易日的「明天盯 / 明天预案」在今日竞价里站在哪。"""
    if prev is None:
        return []
    hits = list(
        session.scalars(
            select(PatternHit).where(
                PatternHit.trade_date == prev,
                PatternHit.pattern.in_(tuple(WUDAO_KEYS)),
            )
        )
    )
    if not hits:
        return []
    codes = [hit.code for hit in hits]
    auctions = _auction_map(session, day, codes)
    dailies = _daily_map(session, prev, codes)

    rows: list[dict[str, Any]] = []
    for hit in hits:
        auction = auctions.get(hit.code)
        daily = dailies.get(hit.code)
        levels = hit.key_levels or {}
        if hit.pattern == "wudao_start":
            key_price = levels.get("breakout") or levels.get("watch_high")
            key_label = "样板高"
        else:
            key_price = levels.get("watch_high")
            key_label = "昨高"
        vs_key = None
        if (
            auction is not None
            and auction.price is not None
            and key_price not in (None, 0)
        ):
            vs_key = round((auction.price / float(key_price) - 1.0) * 100.0, 2)
        detail = hit.detail or {}
        rows.append(
            {
                "code": hit.code,
                "name": hit.name or (auction.name if auction else None),
                "pattern": hit.pattern,
                "pattern_name": WUDAO_KEYS[hit.pattern],
                "score": hit.score,
                "prev_close": (
                    auction.pre_close
                    if auction is not None and auction.pre_close
                    else (daily.close if daily is not None else None)
                ),
                "key_price": key_price,
                "key_label": key_label,
                "auction_price": auction.price if auction else None,
                "auction_pct": auction.pct_chg if auction else None,
                "vs_key_pct": vs_key,
                "above_key": (vs_key is not None and vs_key > 0),
                "break_state": detail.get("break_state"),
            }
        )
    order = {"wudao_sample": 0, "wudao_start": 1}
    rows.sort(key=lambda row: (order.get(row["pattern"], 9), -(row["score"] or 0), row["code"]))
    return rows


def _names(session: Session, codes: list[str]) -> dict[str, str | None]:
    """自选名称：股票 → 转债 → ETF，三张表各查一次（不逐只 get）。"""
    names: dict[str, str | None] = {}
    if not codes:
        return names
    names.update(
        dict(
            session.execute(
                select(StockBasic.code, StockBasic.name).where(StockBasic.code.in_(codes))
            ).all()
        )
    )
    missing = [code for code in codes if code not in names]
    if missing:
        names.update(
            dict(
                session.execute(
                    select(CbBasic.bond_code, CbBasic.bond_name).where(
                        CbBasic.bond_code.in_(missing)
                    )
                ).all()
            )
        )
    missing = [code for code in codes if code not in names]
    if missing:
        names.update(
            dict(
                session.execute(
                    select(EtfBasic.code, EtfBasic.name).where(EtfBasic.code.in_(missing))
                ).all()
            )
        )
    return names


def _watch_rows(
    session: Session, user_id: int | None, day: date
) -> list[dict[str, Any]]:
    """自选盘前摘要：最新本地收盘 + 今日竞价（有则）。"""
    if user_id is None:
        return []
    items = list(
        session.scalars(
            select(Watchlist)
            .where(Watchlist.user_id == user_id)
            .order_by(Watchlist.added_at.desc())
        )
    )
    if not items:
        return []
    codes = [item.code for item in items]
    quotes = latest_quotes(session, codes)
    auctions = _auction_map(session, day, codes)
    names = _names(session, [item.code for item in items if not item.name])

    rows: list[dict[str, Any]] = []
    for item in items:
        quote = quotes.get(item.code)
        auction = auctions.get(item.code)
        rows.append(
            {
                "code": item.code,
                "name": item.name or names.get(item.code),
                "close": quote.close if quote is not None else None,
                "quote_date": quote.trade_date if quote is not None else None,
                "pct_chg": quote.pct_chg if quote is not None else None,
                "auction_pct": auction.pct_chg if auction else None,
                "post_open_pct": _open_pct(auction),
                "tags": item.tags or [],
            }
        )
    return rows


def summary(session: Session, trade_date: date | None = None, user_id: int | None = None) -> dict[str, Any]:
    """盘前聚合。`trade_date` 缺省 = 今天（北京日期）。"""
    day = trade_date or today_cn()
    today = today_cn()
    if day > today:
        raise ValueError(f"{day} 还没到")

    trading = is_trade_day(day, session)
    prev = previous_trade_day(day, session)
    meta = _auction_meta(session, day)

    if not trading:
        note = "今天不是交易日，竞价不会更新；下面是上一交易日的盘后数据。"
    elif not meta["auction_available"]:
        note = "今日竞价未采到 —— 采样在交易日 9:15–9:26 自动跑，9:31 补一次量比。"
    else:
        note = None

    return {
        "trade_date": day,
        "is_trade_day": trading,
        "prev_trade_date": prev,
        **meta,
        "note": note,
        "limit_up": _limit_rows(session, prev, day),
        "wudao": _wudao_rows(session, prev, day),
        "watchlist": _watch_rows(session, user_id, day),
    }
