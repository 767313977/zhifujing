"""资金面两融的「回落到最近一个沪深齐全日」（api/funds._latest_complete_margin_day）。

为什么要有这条测试：2026-10-23 线上实测，收盘当晚 `target`（最新情绪日）当天的两融
一行都没有，而旧代码只按 `target` 精确查 —— 资金面那两张卡片直接空着。补的回落逻辑
有三个容易写错的地方，都是这里要钉住的：

1. 判据是「两个市场都有**融资余额**」，不是「有两个 market 的行」（深市出现过只有行、
   余额为 NULL 的日子）；
2. 只能往前找，不能把比 `target` 更晚的数据当成基准（页面选历史日期时必须这样）；
3. 回落之后对比基准也要跟着回落到「上一个齐全日」，而不是 `target` 的前一交易日。
"""

from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.funds import _latest_complete_margin_day
from app.models import MarginDaily


def _session(tmp_path) -> Session:
    """只有 margin_daily 一张表的临时库 —— 不碰 settings 里那个真库。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'margin.db'}")
    MarginDaily.__table__.create(engine)
    return Session(engine)


def _row(day: date, market: str, balance: float | None = 1.0) -> MarginDaily:
    return MarginDaily(
        trade_date=day,
        market=market,
        financing_balance=balance,
        financing_buy=0.5,
        securities_balance=0.1,
    )


def test_returns_target_when_both_markets_complete(tmp_path):
    day = date(2026, 10, 23)
    with _session(tmp_path) as db:
        db.add_all([_row(day, "sh"), _row(day, "sz")])
        db.commit()
        assert _latest_complete_margin_day(db, day) == day


def test_falls_back_to_last_complete_day_when_target_missing(tmp_path):
    """线上 2026-10-23 的真实情形：当天没有两融行，应回落到 10-22。"""
    target = date(2026, 10, 23)
    prev = date(2026, 10, 22)
    with _session(tmp_path) as db:
        db.add_all([_row(prev, "sh"), _row(prev, "sz"), _row(date(2026, 10, 21), "sh"),
                    _row(date(2026, 10, 21), "sz")])
        db.commit()
        assert _latest_complete_margin_day(db, target) == prev


def test_skips_day_with_one_market_only(tmp_path):
    """只有沪市的那天不算「齐全」，要再往前找。"""
    target = date(2026, 10, 23)
    with _session(tmp_path) as db:
        db.add_all([
            _row(date(2026, 10, 23), "sh"),
            _row(date(2026, 10, 22), "sh"),
            _row(date(2026, 10, 22), "sz"),
        ])
        db.commit()
        assert _latest_complete_margin_day(db, target) == date(2026, 10, 22)


def test_skips_day_whose_balance_is_null(tmp_path):
    """有行但余额为 NULL 的日子同样给不出合计（深市实测出现过）。"""
    target = date(2026, 10, 23)
    with _session(tmp_path) as db:
        db.add_all([
            _row(target, "sh"),
            _row(target, "sz", balance=None),
            _row(date(2026, 10, 22), "sh"),
            _row(date(2026, 10, 22), "sz"),
        ])
        db.commit()
        assert _latest_complete_margin_day(db, target) == date(2026, 10, 22)


def test_ignores_days_after_target(tmp_path):
    """页面选历史日期时，不能把「未来」的数据当基准。"""
    target = date(2026, 10, 21)
    with _session(tmp_path) as db:
        db.add_all([
            _row(date(2026, 10, 23), "sh"),
            _row(date(2026, 10, 23), "sz"),
            _row(target, "sh"),
            _row(target, "sz"),
        ])
        db.commit()
        assert _latest_complete_margin_day(db, target) == target


def test_returns_none_without_any_complete_day(tmp_path):
    with _session(tmp_path) as db:
        db.add_all([_row(date(2026, 10, 23), "sh")])
        db.commit()
        assert _latest_complete_margin_day(db, date(2026, 10, 23)) is None


def test_previous_complete_day_uses_same_rule(tmp_path):
    """`overview` 里算「较上日」时传的是 margin_date - 1 天，这里钉住它的行为。"""
    margin_date = date(2026, 10, 22)
    with _session(tmp_path) as db:
        db.add_all([
            _row(margin_date, "sh"),
            _row(margin_date, "sz"),
            _row(date(2026, 10, 21), "sh"),
            _row(date(2026, 10, 21), "sz"),
        ])
        db.commit()
        assert (
            _latest_complete_margin_day(db, margin_date - timedelta(days=1))
            == date(2026, 10, 21)
        )
