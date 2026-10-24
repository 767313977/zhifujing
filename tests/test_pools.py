"""选股池两块的口径：RPS 复利/分位，首板一进二的过滤与题材热度。"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.jobs.scan_rps import _mid_rank, _ret
from app.models import (
    Base,
    LimitPool,
    PatternHit,
    RpsDaily,
    StockBasic,
    StockConcept,
    StockDaily,
    TradeCalendar,
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

    board = pools.rps_board(session, min_rps=80, mode="all")
    assert board.trade_date == DAY
    assert [row.code for row in board.rows] == ["000001"]
    assert board.rows[0].name == "甲股份"
    assert board.rows[0].close == 12.0
    assert board.rows[0].rps250 == 95.0

    # 不过滤时按 250 日分位降序
    assert [row.code for row in pools.rps_board(session, mode="all").rows] == [
        "000001",
        "000002",
    ]


def _rps_row(session, code: str, rps120, rps250, rps50=None) -> None:
    session.add(
        StockDaily(trade_date=DAY, code=code, close=10.0, pct_chg=1.0, name=code)
    )
    session.add(
        RpsDaily(
            trade_date=DAY,
            code=code,
            ret50=5.0,
            ret120=10.0,
            ret250=20.0,
            rps50=rps50,
            rps120=rps120,
            rps250=rps250,
        )
    )


def test_rps_board_requires_both_windows(session):
    """选股池-1：`min_rps` 必须同时卡 120 / 250 两档，不能只看 RPS250。

    10-23 的实况：RPS250 99 但 RPS120 只有 24 的票混进了「RPS ≥90」榜。
    """
    _rps_row(session, "000001", 95.0, 95.0)
    _rps_row(session, "000002", 50.0, 99.0)  # 只有 250 高
    _rps_row(session, "000003", 95.0, 50.0)  # 只有 120 高
    session.commit()

    board = pools.rps_board(session, min_rps=90, mode="all")
    assert [row.code for row in board.rows] == ["000001"]
    assert sorted(row.code for row in pools.rps_board(session, min_rps=0, mode="all").rows) == [
        "000001",
        "000002",
        "000003",
    ]


def test_rps_board_skips_missing_rps120(session):
    """rps120 为 None（窗口内交易日不足）的不进榜 —— 「≥0」也谈不上。"""
    _rps_row(session, "000001", 95.0, 95.0)
    _rps_row(session, "000002", None, 99.0)
    session.commit()

    assert [row.code for row in pools.rps_board(session, mode="all").rows] == ["000001"]


def test_rps_board_dual_mode(session):
    """「双高」= RPS50 ≥ 90 且 RPS120 ≥ 85，按两者之和降序。"""
    _rps_row(session, "000001", 95.0, 99.0, rps50=95.0)  # 190
    _rps_row(session, "000002", 99.0, 99.0, rps50=99.0)  # 198
    _rps_row(session, "000003", 50.0, 99.0, rps50=99.0)  # 120 不够
    _rps_row(session, "000004", 99.0, 99.0, rps50=60.0)  # 50 日不够
    _rps_row(session, "000005", 95.0, 99.0, rps50=None)  # 没有 50 日值
    session.commit()

    rows = pools.rps_board(session).rows  # 默认 dual
    assert [row.code for row in rows] == ["000002", "000001"]


def test_rps_board_line_red_mode(session):
    """一线红：任一档 ≥ 90 即可，按三档最大值降序。"""
    _rps_row(session, "000001", 50.0, 95.0, rps50=20.0)  # 95
    _rps_row(session, "000002", 96.0, 10.0, rps50=30.0)  # 96
    _rps_row(session, "000003", 80.0, 80.0, rps50=80.0)  # 一条都不红
    session.commit()

    rows = pools.rps_board(session, mode="line_red").rows
    assert [row.code for row in rows] == ["000002", "000001"]


def test_rps_tags_flag_laggards(session):
    """`掉队` = RPS250 很高但 RPS120 < 80（旧榜最前面的那批）。"""
    _rps_row(session, "000001", 40.0, 99.0, rps50=30.0)
    session.commit()

    row = pools.rps_board(session, mode="all").rows[0]
    assert "掉队" in row.tags
    assert "一线红" in row.tags


def test_rps_board_breakout_only(session):
    """只留当天命中了突破类形态的票。"""
    _rps_row(session, "000001", 95.0, 99.0, rps50=95.0)
    _rps_row(session, "000002", 95.0, 99.0, rps50=95.0)
    session.add(PatternHit(trade_date=DAY, code="000001", pattern="new_high", score=80.0))
    session.add(PatternHit(trade_date=DAY, code="000002", pattern="v_bottom", score=80.0))
    session.commit()

    rows = pools.rps_board(session, breakout_only=True).rows
    assert [row.code for row in rows] == ["000001"]
    # 只列突破类形态（`v_bottom` 不在 BREAKOUT_KEYS 里）
    assert len(rows[0].hits) == 1


def test_rps_board_off_high_pct_uses_compounding(session):
    """距新高用 pct_chg 复利：+10% → −10% 的净值是 0.99，距高点 −10%。"""
    days = [date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22), DAY]
    session.add_all(TradeCalendar(trade_date=day) for day in days)
    for day, pct in zip(days, [0.0, 10.0, -10.0, 0.0]):
        session.add(
            StockDaily(
                trade_date=day, code="000001", close=10.0, pct_chg=pct, name="000001"
            )
        )
    session.add(
        RpsDaily(
            trade_date=DAY,
            code="000001",
            ret50=5.0,
            ret120=10.0,
            ret250=20.0,
            rps50=95.0,
            rps120=95.0,
            rps250=99.0,
        )
    )
    session.commit()

    row = pools.rps_board(session).rows[0]
    assert row.off_high_pct == pytest.approx(-10.0, abs=0.01)


def test_first_board_seal_to_amount(session):
    """封成比 = 封单额 / 成交额。"""
    _limit(session, "000001")
    session.add(StockBasic(code="000001", name="甲股份"))
    session.commit()

    row = pools.first_board(session, min_themes=0).rows[0]
    # 5 亿封单 / 8 亿成交额
    assert row.seal_to_amount == 62.5
    assert row.score == 100.0


def test_first_board_min_seal_ratio(session):
    _limit(session, "000001", seal_amount=1 * pools.YI)  # 1 月 / 50 亿 = 2%
    _limit(session, "000002", seal_amount=0.05 * pools.YI)  # 0.1%
    session.add_all(
        [StockBasic(code="000001", name="甲股份"), StockBasic(code="000002", name="乙股份")]
    )
    session.commit()

    assert {row.code for row in pools.first_board(session, min_themes=0).rows} == {
        "000001",
        "000002",
    }
    assert [
        row.code for row in pools.first_board(session, min_themes=0, min_seal_ratio=1).rows
    ] == ["000001"]


def test_first_board_ranks_by_composite_not_theme_only(session):
    """题材家数不再一票否决：封单 1% + 早封的冷门票可以排在封单 0.02% 的热门票前面。"""
    # 热门题材 10 家（候选 A），冷门题材 3 家（候选 B）
    _limit(session, "000001", seal_amount=1 * pools.YI, first_seal_time="140000")
    _limit(session, "000002", seal_amount=20 * pools.YI, first_seal_time="093000")
    for index in range(10):
        session.add(
            StockConcept(trade_date=DAY, code="000001", concept="热门")
            if index == 0
            else StockConcept(trade_date=DAY, code=f"90000{index}", concept="热门")
        )
    for index in range(3):
        session.add(
            StockConcept(trade_date=DAY, code="000002", concept="冷门")
            if index == 0
            else StockConcept(trade_date=DAY, code=f"91000{index}", concept="冷门")
        )
    session.add_all(
        [StockBasic(code="000001", name="甲股份"), StockBasic(code="000002", name="乙股份")]
    )
    session.commit()

    rows = pools.first_board(session, before="150000").rows
    assert [row.code for row in rows] == ["000002", "000001"]
    by_code = {row.code: row for row in rows}
    # A：题材 100 分位、封单 0 分位、最晚封 → 40 分；B：0 / 100 / 100 → 60 分
    assert by_code["000001"].theme_count == 10
    assert by_code["000001"].score == 40.0
    assert by_code["000002"].score == 60.0


def test_first_board_recent_limit_ups_and_true_filter(session):
    """前几天涨停过、断一天再封板的票不是「真首板」。"""
    prior = date(2026, 10, 20)
    session.add_all([TradeCalendar(trade_date=prior), TradeCalendar(trade_date=DAY)])
    _limit(session, "000001", trade_date=prior)  # 3 天前涨停过
    _limit(session, "000001", trade_date=DAY)
    _limit(session, "000002", trade_date=DAY)
    session.add_all(
        [StockBasic(code="000001", name="甲股份"), StockBasic(code="000002", name="乙股份")]
    )
    session.commit()

    by_code = {row.code: row for row in pools.first_board(session, min_themes=0).rows}
    assert by_code["000001"].recent_limit_ups == 1
    assert by_code["000002"].recent_limit_ups == 0

    only_true = pools.first_board(session, min_themes=0, only_true_first_board=True).rows
    assert [row.code for row in only_true] == ["000002"]


def test_first_board_board_shape(session):
    """一字（开=高=低=收）与 T 字（开=高，盘中打开过）。"""
    _limit(session, "000001")
    _limit(session, "000002")
    _limit(session, "000003")
    session.add_all(
        [
            StockBasic(code="000001", name="甲股份"),
            StockBasic(code="000002", name="乙股份"),
            StockBasic(code="000003", name="丙股份"),
        ]
    )
    for code, (open_, high, low, close) in {
        "000001": (11.0, 11.0, 11.0, 11.0),
        "000002": (11.0, 11.0, 10.4, 11.0),
        "000003": (10.2, 11.0, 10.0, 11.0),
    }.items():
        session.add(
            StockDaily(
                trade_date=DAY,
                code=code,
                name=code,
                open=open_,
                high=high,
                low=low,
                close=close,
                pct_chg=10.0,
            )
        )
    session.commit()

    by_code = {
        row.code: row for row in pools.first_board(session, min_themes=0, limit=50).rows
    }
    assert by_code["000001"].board_shape == "一字"
    assert by_code["000002"].board_shape == "T字"
    assert by_code["000003"].board_shape is None


def test_first_board_theme_rank_by_seal_time(session):
    """同一题材里首封最早的票 rank = 1（全部涨停股都参与排名，不只是候选）。"""
    _limit(session, "000001", first_seal_time="093000")
    _limit(session, "000002", first_seal_time="103000")
    # 同题材但不在候选里（首封太晚，被 before 卡掉）的那只也要参与排名
    _limit(session, "000003", first_seal_time="145900")
    for code in ("000001", "000002", "000003"):
        session.add(StockBasic(code=code, name=code))
        session.add(StockConcept(trade_date=DAY, code=code, concept="机器人"))
    session.commit()

    by_code = {
        row.code: row
        for row in pools.first_board(session, min_themes=0, before="140000").rows
    }
    assert by_code["000001"].theme_rank == 1
    assert by_code["000002"].theme_rank == 2
    assert by_code["000001"].theme_count == 3



