"""期指净空走势（api/funds.futures_series）。

要钉住的两件事：

1. **缺数据给 None，不补 0**。`futures_position` 从 2026-10-16 才开始采，60 天窗口里
   绝大多数日期没有行 —— 补 0 会把「没数据」画成「净空归零」，结论正好相反。
2. **合计只在四个品种当天都齐时才给**。否则曲线上会出现一条只含部分品种的「合计」，
   它比单品种线更粗更显眼，最容易被当成真实的全市场净空。

日期轴来自交易日历（不是自然日），所以这里也钉一下「非交易日不出现在 x 轴上」。
"""

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.funds import futures_series
from app.models import FuturesPosition, TradeCalendar


def _session(tmp_path) -> Session:
    """只建 trade_calendar 与 futures_position 两张表 —— 不碰 settings 里那个真库。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'futures.db'}")
    TradeCalendar.__table__.create(engine)
    FuturesPosition.__table__.create(engine)
    return Session(engine)


def _day(db: Session, day: date, values: dict[str, int | None]) -> None:
    """写入某天的持仓行；`values` 里没给的品种就当「当天没发布」。"""
    db.add(TradeCalendar(trade_date=day))
    for variety, net in values.items():
        db.add(
            FuturesPosition(
                trade_date=day,
                variety=variety,
                top_net_short=net,
                top_change=0,
                citic_net_short=0,
                citic_change=0,
            )
        )


def test_total_sums_four_varieties(tmp_path):
    day = date(2026, 10, 26)
    with _session(tmp_path) as db:
        _day(db, day, {"IF": 22398, "IC": 18890, "IH": 15692, "IM": 49794})
        db.commit()

        result = futures_series(days=5, end=day, session=db)

    assert result.dates == [day]
    assert (result.if_net, result.ic_net, result.ih_net, result.im_net) == (
        [22398],
        [18890],
        [15692],
        [49794],
    )
    assert result.total == [22398 + 18890 + 15692 + 49794]


def test_missing_days_are_none_not_zero(tmp_path):
    """没采集到的日子必须是 None：补 0 会把「没数据」画成「净空归零」。"""
    old = date(2026, 10, 15)  # 开始采集之前
    new = date(2026, 10, 16)
    with _session(tmp_path) as db:
        _day(db, old, {})
        _day(db, new, {"IF": 100, "IC": 200, "IH": 300, "IM": 400})
        db.commit()

        result = futures_series(days=5, end=new, session=db)

    assert result.dates == [old, new]
    assert result.if_net == [None, 100]
    assert result.total == [None, 1000]


def test_total_requires_all_four_varieties(tmp_path):
    """少一个品种就不给合计，但已有的品种照常出线。"""
    day = date(2026, 10, 26)
    with _session(tmp_path) as db:
        _day(db, day, {"IF": 100, "IC": 200, "IH": 300})
        db.commit()

        result = futures_series(days=5, end=day, session=db)

    assert result.if_net == [100]
    assert result.im_net == [None]
    assert result.total == [None]


def test_dates_follow_trade_calendar(tmp_path):
    """x 轴只走交易日：周末不在日历里，就算给了行情行也不该出现。"""
    days = [date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)]
    with _session(tmp_path) as db:
        for day in days:
            _day(db, day, {"IF": 1, "IC": 2, "IH": 3, "IM": 4})
        db.commit()

        result = futures_series(days=2, end=days[-1], session=db)

    # days=2 只取最近两个交易日
    assert result.dates == days[-2:]
    assert result.total == [10, 10]
