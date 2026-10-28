"""2026-10-28 第五轮体检修掉的行为，逐条钉住。

这一轮改的都是「口径 / 闸门」类的东西，最容易在以后的改动里被悄悄改回去，
所以每条都配一个反向用例（宁可断言多一点，也别只断言「没抛异常」）。
"""

from datetime import date, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api import deps
from app.api.market import advance_decline
from app.models import Base, SectorDaily, StockDaily, TradeCalendar
from app.services import sector_rps
from app.services.rps import percentiles
from app.sources import akshare_source, base, kaipanhong


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


# ---------------------------------------------------------------- 分位：并列同分


def test_percentiles_ties_share_one_score():
    """三只票题材家数都是 3 —— 必须同分（原来会给出 0/50/100）。"""
    assert percentiles([3, 3, 3]) == [50.0, 50.0, 50.0]


def test_percentiles_ties_use_average_rank():
    """`[1, 2, 2]`：2 那两只并列在第 2、3 名 → 平均名次 1.5 / 2 → 75 分。"""
    assert percentiles([1, 2, 2]) == [0.0, 75.0, 75.0]


def test_percentiles_single_value_and_empty():
    assert percentiles([]) == []
    assert percentiles([7.0]) == [100.0]


# ---------------------------------------------------------------- 板块前瞻收益：半截窗口不算


def _seed_sector_days(session, days: list[date], codes: dict[str, list[float]]) -> None:
    session.add_all(TradeCalendar(trade_date=day) for day in days)
    for code, pcts in codes.items():
        for day, pct in zip(days, pcts, strict=True):
            session.add(
                SectorDaily(
                    trade_date=day,
                    sector_code=code,
                    name=f"板块{code}",
                    taxonomy="kph_selected",
                    pct_chg=pct,
                    member_count=10,
                )
            )
    session.commit()


def test_forward_returns_requires_full_window(session):
    """缺行的板块该档记缺失，不能拿「半截窗口」的收益冒充 5 日收益。"""
    days = [date(2026, 9, 1) + timedelta(days=index) for index in range(8)]
    # A 每天都有；B 的第 3 天（下标 3）缺行
    pcts = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    session.add_all(TradeCalendar(trade_date=day) for day in days)
    for day, pct in zip(days, pcts, strict=True):
        session.add(
            SectorDaily(
                trade_date=day,
                sector_code="A",
                name="全量",
                taxonomy="kph_selected",
                pct_chg=pct,
                member_count=10,
            )
        )
    for index, day in enumerate(days):
        if index == 3:
            continue
        session.add(
            SectorDaily(
                trade_date=day,
                sector_code="B",
                name="缺一天",
                taxonomy="kph_selected",
                pct_chg=5.0,
                member_count=10,
            )
        )
    session.commit()

    out = sector_rps._forward_returns(session, days[0], (5,), "kph_selected")
    assert "A" in out[5]
    assert "B" not in out[5], "缺行的板块不该出现在 5 日窗口里"
    # 5 个 +1% 复利 ≈ 5.10%
    assert out[5]["A"] == pytest.approx(5.101, abs=1e-3)


def test_forward_returns_stops_at_last_available_day(session):
    """交易日历铺到未来、板块日线只到第 3 天 → 5 日窗口根本没走完，一律不给值。"""
    days = [date(2026, 9, 1) + timedelta(days=index) for index in range(10)]
    session.add_all(TradeCalendar(trade_date=day) for day in days)
    for day in days[:3]:
        session.add(
            SectorDaily(
                trade_date=day,
                sector_code="A",
                name="只有三天",
                taxonomy="kph_selected",
                pct_chg=1.0,
                member_count=10,
            )
        )
    session.commit()

    # 信号日 = 第 1 天，之后第 5 个交易日早已越过数据末日
    out = sector_rps._forward_returns(session, days[0], (5,), "kph_selected")
    assert out == {}


# ---------------------------------------------------------------- 涨跌分布：各档之和 == total


def _stock(session, code: str, day: date, pct: float) -> None:
    session.add(
        StockDaily(
            trade_date=day,
            code=code,
            name=code,
            close=10.0,
            pct_chg=pct,
            open=10.0,
            high=10.0,
            low=10.0,
        )
    )


def test_advance_decline_buckets_sum_to_total(session):
    """平盘（0）只能记一档：原来 `-1~0%` 用左开右闭把 0 也数进去了。"""
    day = date(2026, 10, 9)
    session.add(TradeCalendar(trade_date=day))
    samples = [-8.0, -6.0, -4.0, -2.0, -0.5, 0.0, 0.0, 0.5, 2.0, 4.0, 6.0, 8.0]
    for index, pct in enumerate(samples):
        _stock(session, f"6000{index:02d}", day, pct)
    session.commit()

    out = advance_decline(trade_date=day, session=session)
    assert out.total == len(samples)
    assert sum(bucket.count for bucket in out.buckets) == out.total, "各档必须刚好分完"
    flat_bucket = next(bucket for bucket in out.buckets if bucket.label == "平盘")
    down_small = next(bucket for bucket in out.buckets if bucket.label == "-1~0%")
    assert flat_bucket.count == 2
    assert down_small.count == 1  # 只有 -0.5；0 不算


# ---------------------------------------------------------------- 日期依赖：未来交易日要 404


def test_resolve_trade_date_rejects_future_trading_day(session):
    """交易日历预置到年底，`?date=未来交易日` 必须 404 —— 不能回 200 全空。"""
    future = date(2099, 3, 2)
    session.add(TradeCalendar(trade_date=future))
    session.commit()

    with pytest.raises(HTTPException) as raised:
        deps.resolve_trade_date(trade_date=future, session=session)
    assert raised.value.status_code == 404


def test_resolve_trade_date_accepts_past_trading_day(session):
    past = date(2020, 1, 2)
    session.add(TradeCalendar(trade_date=past))
    session.commit()
    assert deps.resolve_trade_date(trade_date=past, session=session) == past


# ------------------------------------------------ 采集：确定性错误不重试 / 只吞特定的 TypeError


def test_deterministic_http_error_only_covers_4xx():
    """4xx 不重试（408/429 除外）；5xx 与网络异常照旧重试。"""
    import requests

    def http_error(code: int) -> requests.HTTPError:
        response = requests.Response()
        response.status_code = code
        return requests.HTTPError(response=response)

    assert base.is_deterministic_http_error(http_error(400)) is True
    assert base.is_deterministic_http_error(http_error(404)) is True
    # 限流与请求超时是明确的「等会儿再来」
    assert base.is_deterministic_http_error(http_error(429)) is False
    assert base.is_deterministic_http_error(http_error(408)) is False
    assert base.is_deterministic_http_error(http_error(500)) is False
    assert base.is_deterministic_http_error(requests.Timeout("慢")) is False


def test_lhb_no_data_only_matches_none_subscript():
    """只把「result 是 null」那一种 TypeError 当无数据，别的照旧抛。"""
    assert akshare_source._is_lhb_no_data(TypeError("'NoneType' object is not subscriptable"))
    assert not akshare_source._is_lhb_no_data(TypeError("not enough values to unpack"))
    assert not akshare_source._is_lhb_no_data(ValueError("'NoneType' object is not subscriptable"))


class _FakeKaipanhong:
    """用假的 `_post` 顶掉网络，只验分页收尾判据。"""

    def __init__(self, pages: dict[str, dict]):
        self.pages = pages
        self.indexes: list[str] = []

    def _post(self, trade_date, params, description):
        index = params["Index"]
        self.indexes.append(index)
        return self.pages[index]


def _member_row(code: str) -> list:
    row = [None] * 11
    row[kaipanhong.MEMBER_CODE] = code
    row[kaipanhong.MEMBER_NAME] = code
    return row


def test_kaipanhong_members_continue_on_short_page(monkeypatch):
    """短页没取满 Count 时继续翻 —— 不能把残缺名单当完整名单。"""
    monkeypatch.setattr(kaipanhong, "MEMBER_PAGE_SIZE", 2)
    fake = _FakeKaipanhong(
        {
            "0": {"Count": 4, "list": [_member_row("600000"), _member_row("600001")]},
            "2": {"Count": 4, "list": [_member_row("600002")]},
            "4": {"Count": 4, "list": [_member_row("600003")]},
        }
    )
    source = object.__new__(kaipanhong.KaipanhongSource)
    source._post = fake._post  # type: ignore[method-assign]

    members = kaipanhong.KaipanhongSource.board_members(source, "P1", date(2026, 10, 27))
    assert [m["code"] for m in members] == ["600000", "600001", "600002", "600003"]
    assert fake.indexes == ["0", "2", "4"], "短页未取满时必须继续，取满才停"


def test_kaipanhong_members_stop_when_short_page_adds_nothing(monkeypatch):
    """短页且这一页没有新人（越界重复吐尾部）→ 立刻收尾，不在 40 页里空转。"""
    monkeypatch.setattr(kaipanhong, "MEMBER_PAGE_SIZE", 2)
    fake = _FakeKaipanhong(
        {
            "0": {"Count": 5, "list": [_member_row("600000"), _member_row("600001")]},
            "2": {"Count": 5, "list": [_member_row("600000")]},
        }
    )
    source = object.__new__(kaipanhong.KaipanhongSource)
    source._post = fake._post  # type: ignore[method-assign]

    members = kaipanhong.KaipanhongSource.board_members(source, "P1", date(2026, 10, 27))
    assert [m["code"] for m in members] == ["600000", "600001"]
    assert fake.indexes == ["0", "2"]
