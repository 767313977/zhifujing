"""S2-C：题材 AND / OR 交叉检索（只读 `sector_member` 快照）。

合成库覆盖：AND/OR、跨 taxonomy、同名不同代码、缺覆盖、跨日不混用、
`stock_concept` 不冒充全市场、分页、参数校验、只读与查询次数。
"""

from datetime import date

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.api import sector as sector_api
from app.models import (
    Base,
    LimitPool,
    SectorBasic,
    SectorMember,
    StockConcept,
)
from app.services import theme_intersection as ti

D1 = date(2026, 11, 10)
D2 = date(2026, 11, 11)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            [
                SectorBasic(code="801001", name="芯片", taxonomy="kph_selected"),
                SectorBasic(code="801002", name="算力", taxonomy="kph_selected"),
                SectorBasic(code="801003", name="半导体", taxonomy="kph_industry"),
                SectorBasic(code="801004", name="无快照板块", taxonomy="kph_selected"),
                # 同名不同代码：不能静默合并
                SectorBasic(code="881001", name="芯片", taxonomy="kph_industry"),
            ]
        )
        db.add_all(
            [
                # D1
                SectorMember(trade_date=D1, sector_code="801001", code="600000", name="浦发银行"),
                SectorMember(trade_date=D1, sector_code="801001", code="000001", name="平安银行"),
                SectorMember(trade_date=D1, sector_code="801001", code="300750", name="宁德时代"),
                SectorMember(trade_date=D1, sector_code="801002", code="000001", name="平安银行"),
                SectorMember(trade_date=D1, sector_code="801002", code="300750", name="宁德时代"),
                SectorMember(trade_date=D1, sector_code="801002", code="830001", name="吉林碳谷"),
                SectorMember(trade_date=D1, sector_code="801003", code="000001", name="平安银行"),
                SectorMember(trade_date=D1, sector_code="801003", code="600519", name="贵州茅台"),
                SectorMember(trade_date=D1, sector_code="881001", code="000001", name="平安银行"),
                SectorMember(trade_date=D1, sector_code="881001", code="600519", name="贵州茅台"),
                # D2：成员变化；801004 只在 D2 有数据
                SectorMember(trade_date=D2, sector_code="801001", code="600000", name="浦发银行"),
                SectorMember(trade_date=D2, sector_code="801001", code="000001", name="平安银行"),
                SectorMember(trade_date=D2, sector_code="801002", code="000001", name="平安银行"),
                SectorMember(trade_date=D2, sector_code="801002", code="300750", name="宁德时代"),
                SectorMember(trade_date=D2, sector_code="801004", code="688001", name="华兴源创"),
            ]
        )
        db.add_all(
            [
                LimitPool(trade_date=D1, code="300750", pool_type="up", consecutive=2),
                LimitPool(trade_date=D1, code="000001", pool_type="up", consecutive=1),
                # 只覆盖涨停股：不能拿它当全市场成员
                StockConcept(trade_date=D1, code="000001", concept="芯片"),
                StockConcept(trade_date=D1, code="999999", concept="芯片"),
            ]
        )
        db.commit()
        yield db


def search(session, **kwargs):
    params = {
        "sector_codes": ["801001", "801002"],
        "mode": "AND",
        "market": "all",
        "as_of": D1,
        "limit": 100,
        "offset": 0,
    }
    params.update(kwargs)
    return ti.search(session, **params)


def test_and_returns_intersection_with_provenance(session):
    data = search(session)
    assert data["resolved_as_of"] == D1
    assert data["is_exact_date"] is True
    assert data["taxonomy"] == "kph_selected"
    assert data["quality"]["status"] == ti.STATUS_VERIFIED
    assert data["total"] == 2
    assert [item["code"] for item in data["items"]] == ["000001", "300750"]

    first = data["items"][0]
    assert first["match_count"] == 2
    assert [hit["code"] for hit in first["sectors"]] == ["801001", "801002"]
    # D1 涨停池里的连板标记
    assert first["limit_up_consecutive"] == 1
    assert data["items"][1]["limit_up_consecutive"] == 2
    # 覆盖：所选板块 2 个都有数据
    assert [item["member_count"] for item in data["selected_sectors"]] == [3, 3]


def test_or_returns_deduplicated_union(session):
    data = search(session, mode="OR")
    assert [item["code"] for item in data["items"]] == [
        "000001",
        "300750",
        "600000",
        "830001",
    ]
    by_code = {item["code"]: item for item in data["items"]}
    assert by_code["000001"]["match_count"] == 2
    assert by_code["600000"]["match_count"] == 1
    assert by_code["830001"]["sectors"] == [{"code": "801002", "name": "算力"}]


def test_three_sector_and_and_or(session):
    and_data = search(session, sector_codes=["801001", "801002", "801003"])
    assert [item["code"] for item in and_data["items"]] == ["000001"]

    or_data = search(session, sector_codes=["801001", "801002", "801003"], mode="OR")
    assert {item["code"] for item in or_data["items"]} == {
        "000001",
        "300750",
        "600000",
        "830001",
        "600519",
    }


def test_missing_sector_makes_and_unverifiable_not_empty_intersection(session):
    data = search(session, sector_codes=["801001", "801004"])
    assert data["quality"]["status"] == ti.STATUS_UNVERIFIABLE
    assert data["items"] == []
    assert "AND_UNVERIFIABLE" in data["quality"]["reasons"]
    assert "SELECTED_SECTOR_NOT_COVERED" in data["warnings"]
    missing = [item for item in data["selected_sectors"] if item["code"] == "801004"][0]
    assert missing["has_data"] is False
    assert missing["member_count"] == 0


def test_missing_sector_keeps_or_partial(session):
    data = search(session, sector_codes=["801001", "801004"], mode="OR")
    assert data["quality"]["status"] == ti.STATUS_PARTIAL
    assert [item["code"] for item in data["items"]] == ["000001", "300750", "600000"]
    assert "SELECTED_SECTOR_NOT_COVERED" in data["warnings"]


def test_as_of_does_not_mix_later_snapshots(session):
    d1 = search(session, sector_codes=["801001"])
    d2 = search(session, sector_codes=["801001"], as_of=D2)
    assert d1["resolved_as_of"] == D1
    assert d2["resolved_as_of"] == D2
    assert [item["code"] for item in d1["items"]] == ["000001", "300750", "600000"]
    assert [item["code"] for item in d2["items"]] == ["000001", "600000"]
    # D2 才出现的成员不能倒灌到 D1
    assert "688001" not in {item["code"] for item in d1["items"]}


def test_as_of_before_first_snapshot_is_not_collected(session):
    data = search(session, as_of=date(2026, 1, 1))
    assert data["resolved_as_of"] is None
    assert data["quality"]["status"] == ti.STATUS_NOT_COLLECTED
    assert data["items"] == []
    assert data["warnings"][0] == ti.NO_COMPLETION_LOG


def test_as_of_falls_back_only_backwards(session):
    data = search(session, as_of=date(2026, 12, 31))
    assert data["resolved_as_of"] == D2
    assert data["is_exact_date"] is False


def test_same_name_different_code_is_not_merged(session):
    data = search(session, sector_codes=["801001", "881001"])
    names = [(item["code"], item["name"]) for item in data["selected_sectors"]]
    assert names == [("801001", "芯片"), ("881001", "芯片")]
    assert data["taxonomy"] == "mixed"
    assert "TAXONOMY_MIXED" in data["warnings"]
    # 两个板块代码都保留在命中来源里
    hit = [item for item in data["items"] if item["code"] == "000001"][0]
    assert [sector["code"] for sector in hit["sectors"]] == ["801001", "881001"]


def test_stock_concept_is_not_used_as_membership(session):
    data = search(session, sector_codes=["801001"], mode="OR")
    codes = {item["code"] for item in data["items"]}
    # stock_concept 里有 999999，但它不是全市场成员 → 不能出现
    assert "999999" not in codes
    # 000001 在成员表里，与 stock_concept 无关也必须在
    assert "000001" in codes


def test_market_filter(session):
    data = search(session, mode="OR", market="sh")
    assert [item["code"] for item in data["items"]] == ["600000"]
    data = search(session, mode="OR", market="sz")
    assert [item["code"] for item in data["items"]] == ["000001", "300750"]
    data = search(session, mode="OR", market="bj")
    assert [item["code"] for item in data["items"]] == ["830001"]


def test_pagination_is_stable(session):
    first = search(session, mode="OR", limit=2, offset=0)
    second = search(session, mode="OR", limit=2, offset=2)
    assert first["total"] == 4
    assert first["shown"] == 2 and first["has_more"] is True
    assert second["shown"] == 2 and second["has_more"] is False
    assert [i["code"] for i in first["items"]] == ["000001", "300750"]
    assert [i["code"] for i in second["items"]] == ["600000", "830001"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sector_codes": []},
        {"sector_codes": [f"8010{index:02d}" for index in range(11)]},
        {"sector_codes": ["bad code!"]},
        {"sector_codes": ["999999"]},
        {"mode": "XOR"},
        {"market": "us"},
        {"limit": 0},
        {"limit": 501},
        {"offset": -1},
    ],
)
def test_invalid_parameters_raise(session, kwargs):
    with pytest.raises(ValueError):
        search(session, **kwargs)


def test_service_is_read_only(session):
    before = (
        session.scalar(select(func.count()).select_from(SectorMember)),
        session.scalar(select(func.count()).select_from(SectorBasic)),
        session.scalar(select(func.count()).select_from(LimitPool)),
        session.scalar(select(func.count()).select_from(StockConcept)),
    )
    search(session, mode="OR")
    after = (
        session.scalar(select(func.count()).select_from(SectorMember)),
        session.scalar(select(func.count()).select_from(SectorBasic)),
        session.scalar(select(func.count()).select_from(LimitPool)),
        session.scalar(select(func.count()).select_from(StockConcept)),
    )
    assert before == after


def test_query_count_is_batched(session):
    from sqlalchemy import event

    counter = {"n": 0}

    def before(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
        counter["n"] += 1

    event.listen(session.get_bind(), "before_cursor_execute", before)
    try:
        search(session, mode="OR")
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", before)
    # 基础信息 / 快照日 / 成员 / 涨停标记 / 3 个覆盖查询，批量后不随成员数增长
    assert counter["n"] <= 10


def test_route_requires_login():
    from app.services.auth import current_user

    route = next(
        item
        for item in sector_api.router.routes
        if getattr(item, "path", "") == "/api/sectors/theme-search"
    )
    calls = {dep.call for dep in route.dependant.dependencies}
    assert current_user in calls
