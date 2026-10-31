"""主线看板（FEAT-THEME-BOARD）：共振门槛、龙头规则、新晋/延续/退潮。

规则写死在 `services/theme_board` 顶部：龙头=连板最高（同板比封单），
中军=剩余里总市值最大，跟风=其余按连板/封单取前 2。
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import (
    Base,
    EtfBasic,
    EtfShare,
    LimitPool,
    SectorDaily,
    StockConcept,
    TradeCalendar,
)
from app.services import theme_board

PREV = date(2026, 10, 29)
DAY = date(2026, 10, 30)


def _pool(day, code, name, consecutive, seal, mv):
    return LimitPool(
        trade_date=day,
        code=code,
        pool_type="up",
        name=name,
        consecutive=consecutive,
        seal_amount=seal,
        total_mv=mv,
    )


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'theme-board.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([TradeCalendar(trade_date=PREV), TradeCalendar(trade_date=DAY)])
        session.add_all(
            [
                _pool(DAY, "300001", "A", 3, 1e8, 5e9),
                _pool(DAY, "300002", "B", 1, 2e8, 2e10),
                _pool(DAY, "300003", "C", 1, 1e8, 1e9),
                _pool(DAY, "300004", "D", 2, 3e8, 3e9),
                _pool(DAY, "300005", "E", 1, 9e7, 8e8),
                _pool(DAY, "300006", "F", 1, 8e7, 7e8),
                _pool(PREV, "300001", "A", 2, 1e8, 5e9),
                _pool(PREV, "300002", "B", 1, 2e8, 2e10),
                _pool(PREV, "300003", "C", 1, 1e8, 1e9),
                _pool(PREV, "300007", "G", 2, 1e8, 2e9),
                _pool(PREV, "300008", "H", 1, 1e8, 1e9),
                _pool(PREV, "300009", "I", 1, 1e8, 1e9),
            ]
        )
        session.add_all(
            [
                StockConcept(trade_date=DAY, code=code, concept="机器人")
                for code in ("300001", "300002", "300003")
            ]
            + [
                StockConcept(trade_date=DAY, code=code, concept="固态电池")
                for code in ("300004", "300005", "300006")
            ]
            + [
                StockConcept(trade_date=PREV, code=code, concept="机器人")
                for code in ("300001", "300002", "300003")
            ]
            + [
                StockConcept(trade_date=PREV, code=code, concept="算力")
                for code in ("300007", "300008", "300009")
            ]
        )
        session.add_all(
            [
                SectorDaily(
                    trade_date=DAY,
                    sector_code="1",
                    name="机器人",
                    taxonomy="kph_selected",
                    pct_chg=6.0,
                    net_inflow=1e8,
                ),
                SectorDaily(
                    trade_date=DAY,
                    sector_code="2",
                    name="固态电池",
                    taxonomy="kph_selected",
                    pct_chg=4.0,
                    net_inflow=5e7,
                ),
                SectorDaily(
                    trade_date=PREV,
                    sector_code="1",
                    name="机器人",
                    taxonomy="kph_selected",
                    pct_chg=2.0,
                ),
                SectorDaily(
                    trade_date=PREV,
                    sector_code="3",
                    name="算力",
                    taxonomy="kph_selected",
                    pct_chg=2.0,
                ),
            ]
        )
        session.commit()
        yield session


def test_mainline_ranks_states_and_fading(db):
    data = theme_board.board(db, DAY)
    assert data["prev_trade_date"] == PREV
    assert data["limit_up_total"] == 6
    rows = {row["concept"]: row for row in data["rows"]}
    assert set(rows) == {"机器人", "固态电池"}
    assert rows["机器人"]["rank"] == 1
    assert rows["机器人"]["state"] == "延续"
    assert rows["机器人"]["rank_change"] == 0
    assert rows["固态电池"]["rank"] == 2
    assert rows["固态电池"]["state"] == "新晋"
    assert rows["固态电池"]["rank_change"] is None
    assert rows["机器人"]["net_inflow"] == 1e8
    assert rows["机器人"]["max_consecutive"] == 3

    fading = {row["concept"]: row for row in data["fading"]}
    assert set(fading) == {"算力"}
    assert fading["算力"]["prev_rank"] == 2
    assert data["note"] is None


def test_leader_roles_follow_documented_rule(db):
    data = theme_board.board(db, DAY)
    robot = next(row for row in data["rows"] if row["concept"] == "机器人")
    roles = [(item["code"], item["role"]) for item in robot["leaders"]]
    assert roles == [
        ("300001", "龙头"),  # 3 板最高
        ("300002", "中军"),  # 剩余里总市值最大（200 亿）
        ("300003", "跟风"),
    ]
    battery = next(row for row in data["rows"] if row["concept"] == "固态电池")
    # 固态电池：300004 两板 → 龙头；剩余里 300005 市值 > 300006 → 中军
    assert [(item["code"], item["role"]) for item in battery["leaders"]] == [
        ("300004", "龙头"),
        ("300005", "中军"),
        ("300006", "跟风"),
    ]


def test_empty_day_returns_note_not_error(db):
    data = theme_board.board(db, date(2026, 10, 31))
    assert data["rows"] == []
    assert data["fading"] == []
    assert data["limit_up_total"] == 0
    assert "没有涨停数据" in (data["note"] or "")


def test_below_threshold_is_not_mainline(db):
    """单题材不足 3 只不算主线（返回空行 + 说明）。"""
    with Session(db.bind) as session:
        session.add_all(
            [
                LimitPool(
                    trade_date=date(2026, 11, 2),
                    code="300010",
                    pool_type="up",
                    name="X",
                    consecutive=1,
                )
            ]
        )
        session.add(
            StockConcept(
                trade_date=date(2026, 11, 2), code="300010", concept="独角兽"
            )
        )
        session.commit()
        data = theme_board.board(session, date(2026, 11, 2))
    assert data["rows"] == []
    assert "共振" in (data["note"] or "")


# ------------------------------------------------------------ 相关 ETF（FEAT-ETF-THEME）


def test_etf_mapping_matches_by_keyword():
    # 板块名常带后缀，按「包含」匹配
    assert theme_board._theme_etf_codes("机器人概念") == ("562500",)
    assert theme_board._theme_etf_codes("半导体设备") == ("512480",)
    # 没有映射的题材返回空（页面不显示这一段，也不猜）
    assert theme_board._theme_etf_codes("独角兽") == ()


def test_etf_flow_attached_only_when_share_data_exists(db):
    with Session(db.bind) as session:
        # 两个快照日各凑够 MIN_ROWS（300 只）才会被当成「覆盖达标」的基准日
        fillers = []
        for day in (PREV, DAY):
            fillers.extend(
                EtfShare(
                    trade_date=day,
                    code=f"5{index:05d}",
                    name=f"填充{index}",
                    shares=100.0,
                    close=1.0,
                )
                for index in range(299)
            )
        session.add_all(fillers)
        session.add_all(
            [
                EtfShare(
                    trade_date=PREV,
                    code="562500",
                    name="机器人ETF",
                    shares=1000.0,
                    close=2.0,
                ),
                EtfShare(
                    trade_date=DAY,
                    code="562500",
                    name="机器人ETF",
                    shares=1100.0,
                    close=2.0,
                ),
                EtfBasic(code="562500", name="机器人ETF"),
            ]
        )
        session.commit()
        data = theme_board.board(session, DAY)

    assert data["etf_date"] == DAY
    robot = next(row for row in data["rows"] if row["concept"] == "机器人")
    # (1100 − 1000) × 2.0 = 200 元净申购
    assert robot["etfs"] == [{"code": "562500", "name": "机器人ETF", "net": 200.0}]
    # 「固态电池」没有映射 → 空列表（页面显示 —）
    battery = next(row for row in data["rows"] if row["concept"] == "固态电池")
    assert battery["etfs"] == []
