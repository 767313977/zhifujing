"""趋势模板参考标签（2026-11-01）：`evaluate` 三态 + `/hits` 与选股池同判据。

钉四件事：
1. 可用日线不足 250 根 → None（不是「不满足」）；
2. 当天没有 `rps250` → None；
3. 其余情况与 `passes` 完全一致（判据只有那一份）；
4. 同一天同一只票，`/hits` 的 `trend_template == True` 当且仅当选股池「趋势模板」
   开关会保留它。
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api import patterns as patterns_api
from app.models import Base, PatternHit, RpsDaily, StockDaily, TradeCalendar
from app.services import pools, trend
from app.services.patterns import build_bars

START = date(2025, 1, 1)
#: 足够判趋势模板：窗口要 250 根，多铺一些让 `load_bars` 的 260 天窗口装得下
BARS = 320
DAYS = [START + timedelta(days=index) for index in range(BARS)]
DAY = DAYS[-1]
PATTERN = "volume_surge"


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'trend.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([TradeCalendar(trade_date=day) for day in DAYS])
        yield session


def _seed(
    session,
    code: str,
    *,
    rising: bool = True,
    bars: int = BARS,
    rps250: float | None = 80.0,
) -> None:
    """铺 `bars` 根单边走势的日线；`rps250` 传 None = 当天没有 RPS 记录。"""
    price = 10.0
    pct = 0.2 if rising else -0.2
    for day in DAYS[:bars]:
        price *= 1 + pct / 100
        session.add(
            StockDaily(
                trade_date=day,
                code=code,
                name=code,
                open=price,
                high=price * 1.01,
                low=price * 0.99,
                close=price,
                volume=1e6,
                amount=1e8,
                pct_chg=pct,
            )
        )
    if rps250 is not None:
        session.add(RpsDaily(trade_date=DAY, code=code, rps250=rps250))
    session.add(
        PatternHit(
            trade_date=DAY,
            code=code,
            pattern=PATTERN,
            name=code,
            score=60.0,
            close=price,
            key_levels={},
            detail={},
        )
    )


def _hits(session):
    return patterns_api.hits(
        db=session,
        trade_date=DAY,
        min_score=0.0,
        pattern=None,
        days=1,
        limit=50,
        order="score",
        phase=None,
    )


def _bars_from(session, code: str):
    """用库里的日线重建一份 Bars（`evaluate` 的单元测试用）。"""
    items = [
        {
            "date": row.trade_date,
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.volume,
            "amount": row.amount,
            "pct_chg": row.pct_chg,
        }
        for row in session.scalars(
            select(StockDaily)
            .where(StockDaily.code == code)
            .order_by(StockDaily.trade_date)
        )
    ]
    return build_bars(items, code)


def test_evaluate_three_states(db):
    session = db
    _seed(session, "300001", rising=True)
    session.commit()
    bars = _bars_from(session, "300001")
    last = len(bars) - 1

    # ① 数据不足 250 根 → None（`passes` 对同样输入返回 False，两者不能混用）
    short = bars.close[: trend.TREND_LOOKBACK - 1]
    short_bars = build_bars(
        [
            {
                "date": DAYS[i],
                "open": float(short[i]),
                "high": float(short[i]),
                "low": float(short[i]),
                "close": float(short[i]),
                "volume": 1e6,
                "amount": 1e8,
                "pct_chg": 0.0,
            }
            for i in range(len(short))
        ],
        "300001",
    )
    assert trend.evaluate(short_bars, len(short_bars) - 1, 80.0) is None
    assert trend.passes(short_bars, len(short_bars) - 1, 80.0) is False

    # ② 当天没有 RPS → None
    assert trend.evaluate(bars, last, None) is None
    # ③ 其余情况与 passes 逐值一致
    assert trend.evaluate(bars, last, 80.0) is True
    for rs in (80.0, 60.0):
        assert trend.evaluate(bars, last, rs) == trend.passes(bars, last, rs)


def test_hits_trend_matches_pool_filter(db):
    session = db
    # 走强的票：数据够、RS 80 → 通过
    _seed(session, "300001", rising=True)
    # 走弱的票：数据够、RS 80 → 不通过
    _seed(session, "300002", rising=False)
    # 上市不足 250 根：无法判断（不是 ✗）
    _seed(session, "300003", rising=True, bars=120)
    # 数据够但当天没有 RPS：同样无法判断
    _seed(session, "300004", rising=True, rps250=None)
    session.commit()

    rows = {row.code: row for row in _hits(session).items}
    assert rows["300001"].trend_template is True
    assert rows["300002"].trend_template is False
    assert rows["300003"].trend_template is None
    assert rows["300004"].trend_template is None

    # 与选股池「趋势模板」开关同判据：池子保留的 == `/hits` 里为 True 的
    pool_rows = [
        pools.RpsRow(
            code=code,
            name=code,
            close=10.0,
            pct_chg=0.0,
            ret50=0.0,
            ret120=0.0,
            ret250=0.0,
            rps50=80.0,
            rps120=80.0,
            rps250=(None if code == "300004" else 80.0),
        )
        for code in ("300001", "300002", "300003", "300004")
    ]
    kept = {row.code for row in pools._filter_trend(session, DAY, pool_rows)}
    assert kept == {row.code for row in rows.values() if row.trend_template is True}
    assert kept == {"300001"}
