"""选股池两块的口径：RPS 复利/分位，首板一进二的过滤与题材热度。"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.jobs.scan_rps import _mid_rank, _ret
from app.models import (
    Base,
    LimitPool,
    RpsDaily,
    StockBasic,
    StockConcept,
    StockDaily,
)
from app.services import pools

DAY = date(2026, 10, 23)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_ret_compounds_and_requires_coverage():
    assert _ret([1.0] * 120, 120) == pytest.approx(round((1.01**120 - 1) * 100, 2))
    # 只有 100 天（120 窗口的 83%）→ 不拿半截窗口冒充
    assert _ret([1.0] * 100, 120) is None
    # 250 窗口同理
    assert _ret([1.0] * 120, 250) is None


def test_mid_rank_is_centered():
    ordered = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert _mid_rank(ordered, 3.0) == 50.0
    assert _mid_rank(ordered, 1.0) == 10.0
    assert _mid_rank(ordered, 5.0) == 90.0
    # 重复值取中位，不把「一片相同」读成极端
    assert _mid_rank([1.0, 1.0, 1.0], 1.0) == 50.0


def _limit(session, code, **kwargs):
    base = {
        "trade_date": DAY,
        "code": code,
        "pool_type": "up",
        "consecutive": 1,
        "open_times": 0,
        "first_seal_time": "093000",
        "float_mv": 50 * pools.YI,
        "amount": 8 * pools.YI,
        "seal_amount": 5 * pools.YI,
        "turnover": 10.0,
        "pct_chg": 10.0,
        "price": 10.0,
    }
    base.update(kwargs)
    session.add(LimitPool(**base))


def test_first_board_filters(session):
    _limit(session, "000001")
    # 14:30 才封板 → 晚于默认的 14:00（排掉尾盘偷袭）
    _limit(session, "000002", first_seal_time="143000")
    _limit(session, "000003", consecutive=2)
    _limit(session, "000004", open_times=1)
    _limit(session, "000005")
    _limit(session, "000006", float_mv=200 * pools.YI)
    _limit(session, "000007")

    session.add_all(
        [
            StockBasic(code="000001", name="甲股份"),
            StockBasic(code="000002", name="乙股份"),
            StockBasic(code="000003", name="丙股份"),
            StockBasic(code="000004", name="丁股份"),
            StockBasic(code="000005", name="ST戊股"),
            StockBasic(code="000006", name="己股份"),
            StockBasic(code="000007", name="庚股份"),
        ]
    )
    session.add_all(
        [
            StockConcept(trade_date=DAY, code="000001", concept="机器人"),
            StockConcept(trade_date=DAY, code="000002", concept="机器人"),
            StockConcept(trade_date=DAY, code="000003", concept="机器人"),
            StockConcept(trade_date=DAY, code="000007", concept="孤门"),
        ]
    )
    session.commit()

    board = pools.first_board(session)
    assert [row.code for row in board.rows] == ["000001"]
    row = board.rows[0]
    assert row.theme == "机器人"
    assert row.theme_count == 3
    assert row.seal_ratio == 10.0  # 5 亿封单 / 50 亿流通


def test_first_board_respects_thresholds(session):
    _limit(session, "000001")
    session.add(StockBasic(code="000001", name="甲股份"))
    session.add(StockConcept(trade_date=DAY, code="000001", concept="机器人"))
    session.commit()

    # 题材家数要求降到 1 → 能入选
    assert pools.first_board(session, min_themes=1).rows[0].code == "000001"
    # 首封时间卡在 09:00 之前 → 排除
    assert pools.first_board(session, before="085959", min_themes=0).rows == []


def test_rps_board_joins_latest_and_filters(session):
    session.add_all(
        [
            StockBasic(code="000001", name="甲股份"),
            StockDaily(
                trade_date=DAY, code="000001", close=12.0, pct_chg=3.0, name="甲股份"
            ),
            StockDaily(
                trade_date=DAY, code="000002", close=8.0, pct_chg=-2.0, name="乙股份"
            ),
            RpsDaily(
                trade_date=DAY,
                code="000001",
                ret120=30.0,
                ret250=80.0,
                rps120=90.0,
                rps250=95.0,
            ),
            RpsDaily(
                trade_date=DAY,
                code="000002",
                ret120=-5.0,
                ret250=10.0,
                rps120=30.0,
                rps250=40.0,
            ),
        ]
    )
    session.commit()

    board = pools.rps_board(session, min_rps=80)
    assert board.trade_date == DAY
    assert [row.code for row in board.rows] == ["000001"]
    assert board.rows[0].name == "甲股份"
    assert board.rows[0].close == 12.0
    assert board.rows[0].rps250 == 95.0

    # 不过滤时按 250 日分位降序
    assert [row.code for row in pools.rps_board(session).rows] == ["000001", "000002"]
