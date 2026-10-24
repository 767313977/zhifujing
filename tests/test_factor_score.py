"""量价因子打分：池内分位加权，以及首板的「按因子分排序」。"""

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, LimitPool, StockBasic, StockDaily, TradeCalendar
from app.services import factor_score, pools

START = date(2026, 9, 1)
DAYS = [START + timedelta(days=index) for index in range(20)]
DAY = DAYS[-1]


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _seed(session, code: str, pct: float, amplitude: float, amount_yi: float) -> None:
    """铺 20 天日线：固定涨跌幅、固定振幅、固定成交额（单位亿）。"""
    close = 10.0
    for day in DAYS:
        session.add(
            StockDaily(
                trade_date=day,
                code=code,
                name=code,
                open=close,
                high=close * (1 + amplitude / 2),
                low=close * (1 - amplitude / 2),
                close=close,
                volume=1e6,
                amount=amount_yi * pools.YI,
                pct_chg=pct,
            )
        )
        close *= 1 + pct / 100


def test_scores_follow_reversal_low_vol_low_liquidity(session):
    session.add_all(TradeCalendar(trade_date=day) for day in DAYS)
    # A：天天涨 + 振幅大 + 成交额小（非流动性最高）
    _seed(session, "000001", pct=1.0, amplitude=0.03, amount_yi=1)
    # B：横盘（中性）
    _seed(session, "000002", pct=0.0, amplitude=0.02, amount_yi=10)
    # C：天天跌 + 振幅小 + 成交额大（反转 + 低波 两项都最好）
    _seed(session, "000003", pct=-1.0, amplitude=0.01, amount_yi=100)
    session.commit()

    scores = factor_score.scores(session, ["000001", "000002", "000003"], DAY)

    # 方向先确认：跌得多的 rev20 为正；振幅越小 amp20 越小
    assert scores["000003"].rev20 > 0 > scores["000001"].rev20
    assert scores["000003"].amp20 < scores["000002"].amp20 < scores["000001"].amp20
    # 加权后 C 最高、A 最低（rev 0.30 + amp 0.40 都站在 C 这边）
    ordered = sorted(scores.values(), key=lambda item: item.score, reverse=True)
    assert [item.code for item in ordered] == ["000003", "000002", "000001"]
    assert ordered[0].score == pytest.approx(85.0)
    assert ordered[-1].score == pytest.approx(30.0)


def test_scores_skip_codes_without_history(session):
    session.add_all(TradeCalendar(trade_date=day) for day in DAYS)
    _seed(session, "000001", pct=1.0, amplitude=0.02, amount_yi=10)
    session.commit()

    scores = factor_score.scores(session, ["000001", "000009"], DAY)
    assert "000001" in scores
    # 没有日线的票不出现在结果里（调用方按 None 处理，不参与因子排序）
    assert "000009" not in scores


def test_first_board_order_by_factor(session):
    """`order="factor"`：入选集合不变，只把顺序换成因子分。"""
    session.add_all(TradeCalendar(trade_date=day) for day in DAYS)
    _seed(session, "000001", pct=1.0, amplitude=0.03, amount_yi=1)
    _seed(session, "000003", pct=-1.0, amplitude=0.01, amount_yi=100)
    for code in ("000001", "000003"):
        session.add(StockBasic(code=code, name=code))
        session.add(
            LimitPool(
                trade_date=DAY,
                code=code,
                pool_type="up",
                consecutive=1,
                open_times=0,
                first_seal_time="093000",
                float_mv=50 * pools.YI,
                amount=8 * pools.YI,
                seal_amount=5 * pools.YI,
                turnover=10.0,
                pct_chg=10.0,
                price=10.0,
            )
        )
    session.commit()

    by_board = pools.first_board(session, min_themes=0)
    assert by_board.rows[0].factor_score is None  # 默认不算因子分

    by_factor = pools.first_board(session, min_themes=0, order="factor")
    assert by_factor.conditions["order"] == "factor"
    assert [row.code for row in by_factor.rows] == ["000003", "000001"]
    assert by_factor.rows[0].factor_score > by_factor.rows[1].factor_score
