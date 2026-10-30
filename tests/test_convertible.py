"""可转债派生指标与筛选（2026-10-30 加）。

钉四件事：

1. **YTM** 与集思录「到期税前收益」对得上（上银转债实测 −12.10%）；
2. **强赎计数**按 `conv_value` 算，转股价下修后跟着变（不依赖正股日线）；
3. 窗口缺数据时 `count_complete=False`，页面据此显示「≥n」；
4. 预设真的生效：双低排除强赎、低溢价按溢价升序、债底要求 YTM > 0。
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, CbBasic, CbDaily, TradeCalendar
from app.services import convertible

DAY = date(2026, 10, 30)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cb.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def add_basic(session, code="113042", **kwargs):
    row = CbBasic(
        bond_code=code,
        bond_name=f"{code}转债",
        stock_code="601229",
        stock_name="上海银行",
        list_date=date(2021, 2, 10),
        maturity_date=date(2027, 1, 25),
        convert_start_date=date(2021, 7, 29),
        issue_size=200.0,
        rating="AAA",
        maturity_call_price=112.0,
        coupon_text=(
            "本次发行可转债票面利率:第一年为0.30%、第二年为0.80%、第三年为1.50%、"
            "第四年为2.80%、第五年为3.50%、第六年为4.00%。"
        ),
        redeem_trigger_pct=130.0,
        resale_trigger_pct=None,
        revise_trigger_pct=85.0,
    )
    for key, value in kwargs.items():
        setattr(row, key, value)
    session.add(row)


def add_daily(session, day, code="113042", conv_value=120.0, **kwargs):
    fields = dict(
        trade_date=day,
        bond_code=code,
        close=115.0,
        pct_chg=0.5,
        amount=2e8,
        remain_size=100.0,
        conv_value=conv_value,
        conv_price=8.05,
        premium_pct=20.0,
        redeem_status=None,
    )
    fields.update(kwargs)
    session.add(CbDaily(**fields))


def fill_calendar(session, end: date, size: int = 30) -> list[date]:
    """从 `end` 往回填 `size` 个「交易日」（跳过周末），返回**升序**。"""
    days: list[date] = []
    cursor = end
    while len(days) < size:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    session.add_all([TradeCalendar(trade_date=day) for day in days])
    return days


def test_parse_coupons_from_real_text():
    text = (
        "本次发行可转债票面利率:第一年为0.30%、第二年为0.80%、第三年为1.50%、"
        "第四年为2.80%、第五年为3.50%、第六年为4.00%。"
    )
    assert convertible.parse_coupons(text) == [0.3, 0.8, 1.5, 2.8, 3.5, 4.0]
    # 解析不出来就空列表 —— 调用方据此把 YTM 记成 None，不拿 0 顶
    assert convertible.parse_coupons("利率见募集说明书") == []
    assert convertible.parse_coupons(None) == []


def test_ytm_matches_jisilu():
    """上银转债 2026-10-30：现价 115.328、到期 2027-01-25、到期赎回价 112。

    这里算的是**复利 IRR**（≈ −11.56%）；集思录那栏「到期税前收益」写 −12.10%，
    是**单利年化**（(112−115.328)/115.328 × 365/87）。两者只在这类「半年内到期」
    的短久期债上差得多，页面口径统一按 IRR 标注。
    """
    coupons = [0.3, 0.8, 1.5, 2.8, 3.5, 4.0]
    value = convertible.ytm(115.328, DAY, date(2027, 1, 25), coupons, 112.0)

    assert value is not None
    assert value == pytest.approx(-11.56, abs=0.05)


def test_ytm_returns_none_without_terms():
    assert convertible.ytm(115.0, DAY, date(2027, 1, 25), [], 112.0) is None
    assert convertible.ytm(115.0, DAY, None, [1.0], 112.0) is None
    assert convertible.ytm(None, DAY, date(2027, 1, 25), [1.0], 112.0) is None


def test_counts_follow_conv_value_and_window_completeness(db):
    days = fill_calendar(db, DAY)
    add_basic(db)
    # 前 25 天都在 130 以下，最近 5 天站上 130：强赎计数应该是 5
    for index, day in enumerate(days):
        add_daily(db, day, conv_value=131.0 if index >= 25 else 120.0)
    db.commit()

    result = convertible.screen(db, DAY, preset="double_low")
    row = next(item for item in result.items if item.bond_code == "113042")

    assert row.redeem_count == 5
    # 30 天全在窗口里 → 计数是精确值
    assert row.redeem_complete is True


def test_counts_are_incomplete_when_window_has_holes(db):
    days = fill_calendar(db, DAY)
    add_basic(db)
    # 只给最近 3 天的数据：窗口里缺 27 天，只能说「≥n」
    for day in days[-3:]:
        add_daily(db, day, conv_value=135.0)
    db.commit()

    result = convertible.screen(db, DAY, preset="double_low")
    row = next(item for item in result.items if item.bond_code == "113042")

    assert row.redeem_count == 3
    assert row.redeem_complete is False


def test_revise_count_uses_new_conv_price(db):
    """转股价下修后 `conv_value` 会跳一下 —— 计数用的是当天落库的转股价值。"""
    days = fill_calendar(db, DAY)
    add_basic(db)
    for index, day in enumerate(days):
        add_daily(db, day, conv_value=80.0 if index >= 20 else 100.0)
    db.commit()

    result = convertible.screen(db, DAY, preset="double_low")
    row = next(item for item in result.items if item.bond_code == "113042")

    # conv_value ≤ 85 的天数：最近 10 天
    assert row.revise_count == 10
    assert "下修临近 10/30" in row.labels


def test_st_risk_tag(db):
    fill_calendar(db, DAY)
    add_basic(db, stock_name="ST 上海")
    add_daily(db, DAY)
    db.commit()

    # 低溢价预设不剔除 ST（只有双低才默认剔），这样才能看到风险标签本身
    result = convertible.screen(db, DAY, preset="low_premium")
    row = next(item for item in result.items if item.bond_code == "113042")

    assert "正股 ST" in row.risks


def test_double_low_preset_excludes_announced_redeem(db):
    fill_calendar(db, DAY)
    add_basic(db, code="113042")
    add_basic(db, code="127110", bond_name="广核转债", stock_code="003816", stock_name="中国广核")
    add_daily(db, DAY, code="113042", premium_pct=5.0)
    add_daily(db, DAY, code="127110", premium_pct=1.0, redeem_status="已公告强赎")
    db.commit()

    result = convertible.screen(db, DAY, preset="double_low")

    codes = [item.bond_code for item in result.items]
    assert codes == ["113042"]
    # 双低 = 现价 + 溢价率
    assert result.items[0].double_low == pytest.approx(120.0)


def test_low_premium_preset_sorts_ascending(db):
    fill_calendar(db, DAY)
    add_basic(db, code="113042")
    add_basic(db, code="127110", bond_name="广核转债")
    add_daily(db, DAY, code="113042", premium_pct=18.0)
    add_daily(db, DAY, code="127110", premium_pct=2.0)
    db.commit()

    result = convertible.screen(db, DAY, preset="low_premium")

    assert [item.bond_code for item in result.items] == ["127110", "113042"]


def test_bond_floor_preset_requires_positive_ytm(db):
    fill_calendar(db, DAY)
    add_basic(db)
    add_daily(db, DAY, ytm_pct=-3.0)
    db.commit()

    result = convertible.screen(db, DAY, preset="bond_floor")

    assert result.items == []


def test_parse_coupons_accepts_arabic_numerals():
    """「第1年 0.3%」这种写法也要认，不然这部分券静默没有 YTM（复查 P2-6）。"""
    text = "第一年0.3%、第2年0.5%、第三年1.0%"
    assert convertible.parse_coupons(text) == [0.3, 0.5, 1.0]


def test_redeem_kind_covers_four_statuses():
    assert convertible.redeem_kind("已公告强赎") == "announced"
    assert convertible.redeem_kind("公告要强赎") == "announced"
    assert convertible.redeem_kind("已满足强赎条件") == "met"
    assert convertible.redeem_kind("公告不强赎") == "no_redeem"
    assert convertible.redeem_kind(None) is None
    assert convertible.redeem_kind("") is None


def test_met_is_not_treated_as_announced(db):
    """「已满足强赎条件」公司还没表态，不能被当成强赎风险剔掉（复查 P1-1）。"""
    fill_calendar(db, DAY)
    add_basic(db)
    add_daily(db, DAY, redeem_status="已满足强赎条件")
    db.commit()

    result = convertible.screen(db, DAY, preset="double_low")

    assert [item.bond_code for item in result.items] == ["113042"]
    labels = result.items[0].labels
    assert "已满足强赎条件" in labels
    assert "已公告强赎" not in labels


def test_jisilu_redeem_days_works_before_window_is_full(db):
    """本地窗口只有 1 天，但集思录的强赎天计数是完整的（复查 P1-4）。

    标签分母是「触发所需天数」（15），不是 30 —— 12/15 与 12/30 的紧迫感完全不同。
    """
    fill_calendar(db, DAY, size=1)
    add_basic(db)
    add_daily(db, DAY, redeem_days=12, redeem_days_required=15)
    db.commit()

    result = convertible.screen(db, DAY, preset="double_low")
    row = result.items[0]

    assert row.redeem_count == 12
    assert row.redeem_complete is True
    assert "强赎临近 12/15" in row.labels


def test_screen_source_takes_majority(db):
    """同一天先走兜底、后来新浪恢复时，source 取出现最多的那个（复查）。"""
    fill_calendar(db, DAY)
    add_basic(db, code="111001")
    add_basic(db, code="111002")
    add_basic(db, code="111003")
    add_daily(db, DAY, code="111001", source="em+ifind")
    add_daily(db, DAY, code="111002", source="sina+em+jsl")
    add_daily(db, DAY, code="111003", source="sina+em+jsl")
    db.commit()

    result = convertible.screen(db, DAY, preset="double_low")

    assert result.source == "sina+em+jsl"


def test_radar_orders_announced_before_met_and_near(db):
    fill_calendar(db, DAY)
    add_basic(db, code="111001", bond_name="公告债")
    add_basic(db, code="111002", bond_name="满足债")
    add_basic(db, code="111003", bond_name="临近债")
    add_daily(
        db,
        DAY,
        code="111001",
        redeem_status="已公告强赎",
        last_trade_date=date(2026, 11, 10),
    )
    add_daily(db, DAY, code="111002", redeem_status="已满足强赎条件", redeem_days=15)
    add_daily(db, DAY, code="111003", redeem_days=12)
    db.commit()

    _, rows = convertible.radar(db, DAY)

    assert [row.bond_code for row in rows] == ["111001", "111002", "111003"]
    assert rows[0].last_trade_date == date(2026, 11, 10)
    assert rows[1].redeem_status == "已满足强赎条件"


def test_t0_preset_scores_and_keeps_cash_gates(db):
    fill_calendar(db, DAY)
    # 到期日放远一点，避免「剩余年限 < 1 年」先把它剔掉，专测成交额这道闸
    add_basic(db, maturity_date=date(2029, 1, 1))
    add_daily(db, DAY, amount=4e7)  # 成交额 4000 万 < 5000 万，必须被剔
    db.commit()

    result = convertible.screen(db, DAY, preset="t0")

    assert result.items == []
    assert result.filters["amount_min"] == 50_000_000.0

