"""竞价强度接口（`api/auction.py`）的派生指标与排序口径。

这一层的数字全部是**现算**的（库里没有这些列），所以最容易在改动里被悄悄改错。
这里自己造帧数据，把四处口径钉住：

1. `_amplitude`：竞价价格波动率（至少 2 帧、昨收有效）；
2. 9:20 撤单率：两帧「涨停价上的挂单量」之差；
3. 封成比：涨停价封单额 ÷ 自由流通市值；
4. 派生列排序方向（含「缺值恒排最后」）。

外加一条贯穿全文件的约定：**取不到就给 None，绝不拿 0 顶替** —— 页面显示「—」。
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api import auction
from app.models import (
    SAMPLE_ASK1_PRICE,
    SAMPLE_ASK1_VOLUME,
    SAMPLE_BID1_PRICE,
    SAMPLE_BID1_VOLUME,
    SAMPLE_PRICE,
    AuctionSnapshot,
    Base,
    StockBasic,
)

DAY = date(2026, 10, 27)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    # 行业映射是进程内缓存（6 小时），别让上一个用例的空表污染这一个
    auction._industry_cache = None
    with Session(engine) as session:
        yield session
    auction._industry_cache = None


def frame(
    hhmm: str,
    price: float | None,
    *,
    bid1: float | None = None,
    bid1_vol: float | None = None,
    ask1: float | None = None,
    ask1_vol: float | None = None,
) -> list:
    """按 `SAMPLE_*` 的下标造一帧（8 列，与 2026-10-12 之后的写入格式一致）。"""
    row: list = [None] * 8
    row[0] = hhmm
    row[SAMPLE_PRICE] = price
    row[SAMPLE_BID1_PRICE] = bid1
    row[SAMPLE_BID1_VOLUME] = bid1_vol
    row[SAMPLE_ASK1_PRICE] = ask1
    row[SAMPLE_ASK1_VOLUME] = ask1_vol
    return row


def snapshot(**kwargs) -> AuctionSnapshot:
    """未落库的快照对象（`_metrics` 只读属性，不需要 flush）。"""
    base = {
        "trade_date": DAY,
        "code": "600000",
        "name": "浦发银行",
        "price": 10.0,
        "pre_close": 10.0,
        "pct_chg": 0.0,
        "amount": 0.0,
        "volume": 0.0,
    }
    base.update(kwargs)
    return AuctionSnapshot(**base)


# ------------------------------------------------------------ 竞价价格波动率


def test_amplitude_uses_high_low_of_all_frames():
    samples = [frame("0915", 10.0), frame("0920", 10.5), frame("0925", 9.5)]
    # (10.5 − 9.5) ÷ 10 × 100
    assert auction._amplitude(samples, 10.0) == 10.0


def test_amplitude_needs_two_frames():
    """一帧谈不上「波动」—— 必须给 None，不能拿 0 冒充「没波动」。"""
    assert auction._amplitude([frame("0925", 10.3)], 10.0) is None
    assert auction._amplitude([], 10.0) is None
    assert auction._amplitude(None, 10.0) is None


def test_amplitude_needs_valid_pre_close():
    samples = [frame("0915", 10.0), frame("0920", 10.5)]
    assert auction._amplitude(samples, None) is None
    assert auction._amplitude(samples, 0) is None


def test_amplitude_ignores_frames_without_price():
    """老行 / 空帧的缺值不参与最高最低（否则 max() 会抛或算出假区间）。"""
    samples = [frame("0915", None), frame("0916", 10.0), frame("0917", 10.4)]
    assert auction._amplitude(samples, 10.0) == 4.0


# ------------------------------------------------------------ 9:20 撤单率 / 封成比


def metrics_of(session: Session, row: AuctionSnapshot) -> dict:
    return auction._metrics(session, DAY, [row])[row.code]


def test_cancel_rate_compares_open_volume_before_and_after_0920(session):
    row = snapshot(
        price=11.0,
        pre_close=10.0,
        limit_up=11.0,
        samples=[
            # 9:20 涨停价上挂着 1000 股，9:21 只剩 400 股 → 撤了 60%
            frame("0920", 11.0, bid1=11.0, bid1_vol=1000.0),
            frame("0921", 11.0, bid1=11.0, bid1_vol=400.0),
        ],
    )
    assert metrics_of(session, row)["cancel_rate"] == 60.0


def test_cancel_rate_negative_when_orders_grow(session):
    row = snapshot(
        price=11.0,
        limit_up=11.0,
        samples=[
            frame("0920", 11.0, bid1=11.0, bid1_vol=900.0),
            frame("0921", 11.0, bid1=11.0, bid1_vol=1000.0),
        ],
    )
    # 负值 = 挂单还在增加（不是撤单），口径写清楚，别翻成 0
    assert metrics_of(session, row)["cancel_rate"] == pytest.approx(-11.11, abs=1e-2)


def test_cancel_rate_none_when_either_frame_missing(session):
    only_before = snapshot(
        price=11.0,
        limit_up=11.0,
        samples=[frame("0920", 11.0, bid1=11.0, bid1_vol=1000.0)],
    )
    assert metrics_of(session, only_before)["cancel_rate"] is None

    # 普通票：买一价不是涨停价 → 没有「涨停价上的封单」可言
    normal = snapshot(
        price=10.2,
        limit_up=11.0,
        samples=[
            frame("0920", 10.2, bid1=10.19, bid1_vol=1000.0),
            frame("0921", 10.2, bid1=10.19, bid1_vol=400.0),
        ],
    )
    assert metrics_of(session, normal)["cancel_rate"] is None


def test_cancel_rate_ignores_negative_limit_sentinel(session):
    """历史行用 −1 表示「无涨跌停」；−1 是 truthy，不拦会让判据恒真。"""
    row = snapshot(
        price=10.0,
        limit_up=-1,
        samples=[
            frame("0920", 10.0, bid1=10.0, bid1_vol=1000.0),
            frame("0921", 10.0, bid1=10.0, bid1_vol=400.0),
        ],
    )
    assert metrics_of(session, row)["cancel_rate"] is None


def test_seal_ratio_is_seal_amount_over_free_float_mv(session):
    # 自由流通股 1 亿 × 竞价价 11 元 = 11 亿自由流通市值；
    # 封单 1,000,000 股 × 11 元 = 1100 万 → 封成比 1.0%
    session.add(StockBasic(code="600000", name="浦发银行", free_float_shares=1e8))
    session.commit()
    row = snapshot(
        price=11.0,
        pre_close=10.0,
        limit_up=11.0,
        samples=[frame("0925", 11.0, bid1=11.0, bid1_vol=1_000_000.0)],
    )
    assert metrics_of(session, row)["seal_ratio"] == 1.0


def test_seal_ratio_none_when_not_sealed_or_no_float_shares(session):
    # 没封在涨停价上
    open_board = snapshot(
        price=10.4,
        limit_up=11.0,
        samples=[frame("0925", 10.4, bid1=10.4, bid1_vol=1_000_000.0)],
    )
    assert metrics_of(session, open_board)["seal_ratio"] is None

    # 封住了但没有自由流通股（算不出自由流通市值）→ None，不是 0
    no_shares = snapshot(
        price=11.0,
        limit_up=11.0,
        samples=[frame("0925", 11.0, bid1=11.0, bid1_vol=1_000_000.0)],
    )
    assert metrics_of(session, no_shares)["seal_ratio"] is None


# ------------------------------------------------------------ 派生列排序


def add_snapshot(
    session: Session, code: str, prices: list[float], *, pre_close: float = 10.0
) -> None:
    """按给出的逐帧价格落一行（波动率由这些价格决定）。"""
    session.add(
        AuctionSnapshot(
            trade_date=DAY,
            code=code,
            name=code,
            price=prices[-1],
            pre_close=pre_close,
            pct_chg=0.0,
            amount=1000.0,
            volume=1000.0,
            samples=[frame(f"09{15 + index}", price) for index, price in enumerate(prices)],
        )
    )


def query_hits(session: Session, sort: str, order: str) -> list[str]:
    """直接调接口函数（参数全给，避开 FastAPI 的 Query 默认值）。"""
    out = auction.auction_list(
        trade_date=DAY,
        sort=sort,
        order=order,
        min_pct=None,
        max_pct=None,
        min_amount=None,
        max_amount=None,
        min_ratio=None,
        max_ratio=None,
        min_turnover=None,
        max_turnover=None,
        min_boards=None,
        industry=None,
        limit=10,
        session=session,
    )
    return [row.code for row in out.rows]


def test_derived_sort_amplitude_both_directions_blank_last(session):
    # 波动率：600001 = 10%（9.5~10.5）、600000 = 5%（9.75~10.25）、600002 = 2%；
    # 600003 只有一帧 → 波动率缺失（None）
    add_snapshot(session, "600000", [9.75, 10.25])
    add_snapshot(session, "600001", [9.5, 10.5])
    add_snapshot(session, "600002", [9.9, 10.1])
    add_snapshot(session, "600003", [10.0])
    session.commit()

    assert query_hits(session, "amplitude", "desc") == [
        "600001",
        "600000",
        "600002",
        "600003",
    ]
    # 升序也是「缺值排最后」—— 不跟着方向翻到最前
    assert query_hits(session, "amplitude", "asc") == [
        "600002",
        "600000",
        "600001",
        "600003",
    ]


# ------------------------------------------------------------ 缺数据给 None，不给 0


def test_missing_values_are_none_not_zero(session):
    """干净库里没有任何可算的字段：全部 None（前端显示「—」）。

    这一条是反面用例：如果哪天有人「顺手」把 None 换成 0，页面会把
    「没采到」读成「波动 0% / 撤单 0 / 封成比 0」。
    """
    session.add(
        AuctionSnapshot(
            trade_date=DAY,
            code="600000",
            name="浦发银行",
            price=10.0,
            pre_close=None,
            pct_chg=None,
            open=0,
            volume=None,
            volume_ratio=0,
            limit_up=-1,
            limit_down=-1,
            samples=None,
        )
    )
    session.commit()

    out = auction.auction_list(
        trade_date=DAY,
        sort="amount",
        order="desc",
        min_pct=None,
        max_pct=None,
        min_amount=None,
        max_amount=None,
        min_ratio=None,
        max_ratio=None,
        min_turnover=None,
        max_turnover=None,
        min_boards=None,
        industry=None,
        limit=10,
        session=session,
    )
    assert len(out.rows) == 1
    row = out.rows[0]
    assert row.amplitude is None
    assert row.cancel_rate is None
    assert row.seal_ratio is None
    assert row.free_turnover is None
    assert row.amount_ratio is None
    assert row.free_float_ratio is None
    # 展示层的脏值清洗：0 的「今开 / 量比」与 −1 的涨跌停哨兵都归 None
    assert row.open is None
    assert row.volume_ratio is None
    assert row.limit_up is None
    assert row.limit_down is None
