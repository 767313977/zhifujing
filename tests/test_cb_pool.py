"""可转债 T+0 观察池（2026-10-30 加）。

钉四件事：

1. **硬门槛的边界**：强赎 / 价格 / 溢价 / 成交额 / 剩余规模 / 期限 / 评级 / 新债，
   每条都得按阈值剔人，且顺序固定（页面只显示第一条原因）；
2. **剩余规模未知不冒充**：集思录没这条记录时不能拿发行规模顶替 —— 两个数差的就是
   「还剩多少没转股」，顶替会把 0.5 亿的小盘债当成大盘债放进来；
3. **打分**：强正股 + 高换手 + 低溢价排前面；某一项缺数据时按剩余权重归一，
   不能按 0 分算（那是把「没数据」当成「最差」）；
4. **采集器只认今天**：三个来源都只有实时行情，写进旧日期会让百分位全错。
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.jobs.collect_cb import _merge, refresh
from app.models import Base, CbDaily
from app.services import cb_pool

DAY = date(2026, 10, 30)


def make_row(code: str, **kwargs) -> CbDaily:
    """一只「全部通过门槛」的转债，各用例只覆盖自己关心的字段。"""
    base = dict(
        trade_date=DAY,
        code=code,
        name=f"{code}转债",
        stock_code=f"00{code[-4:]}",
        stock_name="测试正股",
        close=120.0,
        prev_close=119.0,
        pct_chg=0.8,
        high=121.5,
        low=118.5,
        amount=1e8,
        amplitude=2.5,
        convert_price=10.0,
        convert_value=118.0,
        premium_rt=2.0,
        issue_size=10.0,
        rating="AA",
        list_date=date(2024, 1, 1),
        remain_size=8.0,
        turnover_rate=1.25,
        maturity_date=date(2029, 1, 1),
        redeem_status=None,
        stock_close=11.8,
        stock_pct_chg=1.0,
    )
    base.update(kwargs)
    return CbDaily(**base)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cb.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_reject_reason_covers_each_gate():
    day = DAY
    assert cb_pool.reject_reason(make_row("111001"), day) is None
    assert cb_pool.reject_reason(make_row("111002", redeem_status="已公告强赎"), day) == "强赎风险"
    assert cb_pool.reject_reason(make_row("111003", redeem_status="已满足强赎条件"), day) == "强赎风险"
    # 「公告不强赎」不是风险，要放行
    assert cb_pool.reject_reason(make_row("111004", redeem_status="公告不强赎"), day) is None
    assert cb_pool.reject_reason(make_row("111005", close=99.0), day) == "价格<100"
    assert cb_pool.reject_reason(make_row("111006", close=151.0), day) == "价格>150"
    assert cb_pool.reject_reason(make_row("111007", premium_rt=31.0), day) == "溢价>30%"
    assert cb_pool.reject_reason(make_row("111008", premium_rt=None), day) == "缺溢价率"
    assert cb_pool.reject_reason(make_row("111009", amount=4e7), day) == "成交额<0.50亿"
    assert cb_pool.reject_reason(make_row("111010", remain_size=2.9), day) == "剩余<3亿"
    assert cb_pool.reject_reason(make_row("111011", remain_size=None), day) == "剩余规模未知"
    assert (
        cb_pool.reject_reason(make_row("111012", maturity_date=date(2027, 6, 1)), day)
        == "剩余期限<1年"
    )
    assert cb_pool.reject_reason(make_row("111013", rating="A+"), day) == "评级低于AA-"
    # 定向转债的 `sti` 后缀不影响评级判定
    assert cb_pool.reject_reason(make_row("111014", rating="AA+sti"), day) is None
    assert cb_pool.reject_reason(make_row("111015", stock_name="ST 测试"), day) == "ST / 退市风险"
    assert (
        cb_pool.reject_reason(make_row("111016", list_date=date(2026, 10, 28)), day)
        == "新债上市未满5日"
    )


def test_build_splits_passed_and_excluded(db):
    db.add_all(
        [
            make_row("111001", amount=3e8, stock_pct_chg=3.0, turnover_rate=5.0),
            make_row("111002", amount=1e8, stock_pct_chg=1.0, turnover_rate=2.0),
            make_row("111003", redeem_status="已公告强赎", amount=9e8),
            make_row("111004", premium_rt=80.0, amount=2e8),
        ]
    )
    db.commit()

    pool = cb_pool.build(db)

    assert pool.trade_date == DAY
    assert pool.total == 4
    assert {row.code for row in pool.rows} == {"111001", "111002"}
    # 正股更强、换手更高、成交额更大的排前面
    assert [row.code for row in pool.rows] == ["111001", "111002"]
    # 剔除表按成交额降序：强赎那只成交额最大，排第一
    assert [row.code for row in pool.excluded] == ["111003", "111004"]
    assert {gate.label: gate.count for gate in pool.gates} == {"强赎风险": 1, "溢价>30%": 1}


def test_score_renormalizes_when_a_field_is_missing(db):
    """正股涨跌幅缺失时按剩余权重归一，不能按 0 分算成池底。"""
    db.add_all(
        [
            make_row("111001", stock_pct_chg=None, turnover_rate=9.0, premium_rt=0.5),
            make_row("111002", stock_pct_chg=5.0, turnover_rate=1.0, premium_rt=20.0),
        ]
    )
    db.commit()

    pool = cb_pool.build(db)
    scores = {row.code: row.score for row in pool.rows}

    # 缺一项的那只仍有 0~100 的分数，而不是被压成 0
    assert 0 < scores["111001"] < 100


def test_double_low_is_price_plus_premium(db):
    db.add(make_row("111001", close=110.0, premium_rt=12.5))
    db.commit()

    pool = cb_pool.build(db)

    assert pool.rows[0].double_low == pytest.approx(122.5)


def test_unknown_date_returns_empty_without_falling_back(db):
    db.add(make_row("111001"))
    db.commit()

    pool = cb_pool.build(db, date(2020, 1, 2))

    assert pool.trade_date is None
    assert pool.rows == []


# ---------------------------------------------------------------- 采集器


def test_merge_joins_three_sources_and_skips_zero_price():
    spot = [
        {
            "code": "127110",
            "name": "广核转债",
            "trade": "133.11",
            "settlement": "135.70",
            "changepercent": "-1.91",
            "high": "135.999",
            "low": "132.50",
            "amount": "392608424",
        },
        # 北交所定向转债：恒为 0，没有观察意义
        {"code": "810011", "name": "优机定转", "trade": "0", "settlement": "99.8"},
    ]
    basic = [
        {
            "债券代码": "127110",
            "债券简称": "广核转债",
            "正股代码": "003816",
            "正股简称": "中国广核",
            "正股价": "4.79",
            "转股价": "3.58",
            "转股价值": "133.7989",
            "转股溢价率": "-0.51",
            "发行规模": "49",
            "信用评级": "AAA",
            "上市时间": "2025-07-25",
        }
    ]
    redeem = [
        {
            "代码": "127110",
            "剩余规模": "48.482",
            "到期日": "2031-07-09",
            "最后交易日": "2031-06-25",
            "强赎状态": None,
        }
    ]

    rows = _merge(spot, basic, redeem)

    assert [row["code"] for row in rows] == ["127110"]
    row = rows[0]
    assert row["close"] == pytest.approx(133.11)
    assert row["amplitude"] == pytest.approx(round((135.999 - 132.5) / 135.70 * 100, 2))
    assert row["premium_rt"] == pytest.approx(-0.51)
    assert row["remain_size"] == pytest.approx(48.482)
    # 换手率 = 成交额 ÷ 剩余规模
    assert row["turnover_rate"] == pytest.approx(round(392608424 / (48.482 * 1e8) * 100, 2))
    assert row["rating"] == "AAA"
    assert row["list_date"] == date(2025, 7, 25)
    assert row["stock_pct_chg"] is None  # 正股涨跌幅要另外按 stock_daily 补


def test_merge_does_not_fake_remain_size_from_issue_size():
    """集思录没这条记录时，`remain_size` 必须是 None（不能拿发行规模顶替）。"""
    spot = [
        {"code": "123001", "name": "某转债", "trade": "110", "settlement": "109", "amount": "9e7"}
    ]
    basic = [{"债券代码": "123001", "发行规模": "8", "信用评级": "AA"}]

    row = _merge(spot, basic, [])[0]

    assert row["remain_size"] is None
    assert row["turnover_rate"] is None
    assert row["issue_size"] == pytest.approx(8.0)


def test_refresh_refuses_non_today():
    with pytest.raises(RuntimeError, match="只能当天采"):
        refresh(trade_date=date(2020, 1, 2))
