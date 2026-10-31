"""可转债采集（2026-10-30 加）。

钉四件事（对应指令清单可转债-1 的验收）：

1. `"-"` / 空串 / NaN 一律转 None，**不能写成 0**；
2. 未上市的新债只进 `cb_basic`，不进 `cb_daily`；
3. 覆盖率不足时 raise，且整天不写入；
4. 从行情里消失的券**保留在 basic**，只写 `delist_date`。
"""

from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.jobs import collect_cb
from app.jobs.collect_cb import _fill_terms as fill_terms_real
from app.models import Base, CbBasic, CbDaily
from app.sources.ifind import to_bond_symbol

DAY = date(2026, 10, 30)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """把采集器接到临时库上，并把它「今天」固定成 DAY。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'cb.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope():
        session = maker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(collect_cb, "session_scope", scope)
    monkeypatch.setattr(collect_cb, "today_cn", lambda: DAY)
    # 条款是单券请求，这里不测
    monkeypatch.setattr(collect_cb, "_fill_terms", lambda *args, **kwargs: 0)
    return engine, maker, monkeypatch


def spot_row(code="127110", price="133.11", **kwargs):
    row = {
        "code": code,
        "name": "广核转债",
        "trade": price,
        "settlement": "135.70",
        "changepercent": "-1.91",
        "high": "135.999",
        "low": "132.50",
        "amount": "392608424",
    }
    row.update(kwargs)
    return row


def cov_row(code="127110", **kwargs):
    row = {
        "债券代码": code,
        "债券简称": "广核转债",
        "正股代码": "003816",
        "正股简称": "中国广核",
        "正股价": "4.79",
        "转股价": "3.58",
        "转股价值": "133.7989",
        "转股溢价率": "-0.51",
        "债现价": "133.11",
        "发行规模": "49",
        "信用评级": "AAA",
        "上市时间": "2025-07-25",
    }
    row.update(kwargs)
    return row


def redeem_row(code="127110", **kwargs):
    row = {
        "代码": code,
        "剩余规模": "48.482",
        "到期日": "2031-07-09",
        "最后交易日": "2031-06-25",
        "强赎状态": None,
        "强赎天计数": "12/15 | 30",
    }
    row.update(kwargs)
    return row


class FakeSource:
    def __init__(self, spot, cov, redeem, settings=None):
        self._spot = spot
        self._cov = cov
        self._redeem = redeem

    def cb_spot(self):
        return self._spot

    def cb_basic(self):
        return self._cov

    def cb_redeem(self):
        return self._redeem

    def cb_terms(self, bond_code):
        return {}


def install(monkeypatch, spot, cov, redeem):
    monkeypatch.setattr(
        collect_cb, "AkshareSource", lambda settings=None: FakeSource(spot, cov, redeem)
    )


def test_num_and_date_normalisation():
    assert collect_cb._num("-") is None
    assert collect_cb._num("") is None
    assert collect_cb._num("nan") is None
    assert collect_cb._num("1,234.5") == pytest.approx(1234.5)
    assert collect_cb._date("") is None
    assert collect_cb._date("2025-07-25") == date(2025, 7, 25)


def test_call_price_falls_back_to_clause_text():
    """`EXECUTE_PRICE_SH` 只有发生强赎 / 摘牌的券才有（371 只里仅 66 只），
    所以要从条款原文里抠「面值的 112%」。"""
    assert collect_cb.call_price_from_terms({"EXECUTE_PRICE_SH": 112}) == 112
    clause = (
        "到期赎回条款在本次发行的可转债期满后五个交易日内，公司将以本次发行的可转债的"
        "票面面值的112%（含最后一期计息年度利息）的价格向投资者赎回全部未转股的可转债。"
    )
    assert collect_cb.call_price_from_terms({"REDEEM_CLAUSE": clause}) == 112.0
    assert collect_cb.call_price_from_terms({"REDEEM_CLAUSE": "面值的 108% 赎回"}) == 108.0
    assert collect_cb.call_price_from_terms({}) is None


def test_basic_rows_skip_unlisted():
    rows = collect_cb._basic_rows(
        [cov_row(), cov_row(code="123291", **{"上市时间": None})],
        {"127110": redeem_row()},
    )

    assert list(rows) == ["127110"]
    assert rows["127110"]["issue_size"] == pytest.approx(49.0)
    # 到期日取自集思录强赎表（每天都有），不必等条款补齐
    assert rows["127110"]["maturity_date"] == date(2031, 7, 9)


def test_merge_daily_skips_zero_price_and_computes_derived():
    basics = {
        "127110": CbBasic(bond_code="127110", list_date=date(2025, 7, 25)),
    }
    rows = collect_cb._merge_daily(
        [
            spot_row(),
            # 北交所定向转债：价格恒为 0，没有观察意义
            spot_row(code="810011", price="0"),
        ],
        {"127110": cov_row()},
        {"127110": redeem_row()},
        basics,
        DAY,
    )

    assert [row["bond_code"] for row in rows] == ["127110"]
    row = rows[0]
    assert row["amplitude"] == pytest.approx(round((135.999 - 132.5) / 135.70 * 100, 2))
    assert row["turnover"] == pytest.approx(round(392608424 / (48.482 * 1e8) * 100, 2))
    assert row["ytm_pct"] is None  # 票息补齐之前不猜


def test_merge_daily_does_not_fake_remain_size():
    basics = {"123001": CbBasic(bond_code="123001", list_date=date(2025, 1, 1))}
    rows = collect_cb._merge_daily(
        [spot_row(code="123001", price="110", settlement="109", amount="9e7")],
        {"123001": cov_row(code="123001", **{"转股价值": None, "转股价": "10", "正股价": "11"})},
        {},
        basics,
        DAY,
    )

    row = rows[0]
    assert row["remain_size"] is None
    assert row["turnover"] is None
    # 东财没给转股价值时按 100 ÷ 转股价 × 正股价 自己算
    assert row["conv_value"] == pytest.approx(110.0)


def test_bond_symbol_exchange_suffix():
    """转债不能套股票的前缀规则：11 开头是沪市，不是深市。"""
    assert to_bond_symbol("113042") == "113042.SH"
    assert to_bond_symbol("110077") == "110077.SH"
    assert to_bond_symbol("118001") == "118001.SH"
    assert to_bond_symbol("127110") == "127110.SZ"
    assert to_bond_symbol("123076") == "123076.SZ"


def test_spot_from_cov_uses_bond_full_price():
    rows = collect_cb._spot_from_cov(
        [
            {"债券代码": "127110", "债券简称": "广核转债", "债现价": "133.11", "上市时间": "2025-07-25"},
            # 未上市：上市日为空，债现价固定 100，必须剔掉
            {"债券代码": "123291", "债券简称": "待上市", "债现价": 100.0, "上市时间": None},
            # 已退市：债现价也停在 100，且不在昨日名单里
            {"债券代码": "110001", "债券简称": "老债", "债现价": 100.0, "上市时间": "2018-01-01"},
            {"债券代码": "123292", "债券简称": "无价", "债现价": None, "上市时间": "2025-01-01"},
        ],
        DAY,
        {"127110", "123292"},
    )

    assert [row["code"] for row in rows] == ["127110"]
    assert rows[0]["trade"] == pytest.approx(133.11)
    # 涨跌幅 / 成交额兜底时没有，留 None（页面显示「—」），不能写 0
    assert rows[0]["changepercent"] is None
    assert rows[0]["amount"] is None


def test_load_spot_falls_back_to_em_price_plus_ifind_amount(monkeypatch):
    class BrokenSina:
        def cb_spot(self):
            raise RuntimeError("新浪挂了")

    monkeypatch.setattr(collect_cb, "MIN_SPOT_ROWS", 1)
    monkeypatch.setattr(collect_cb, "_recent_universe", lambda: {"127110", "113042"})
    monkeypatch.setattr(collect_cb, "_ifind_allowed", lambda settings, day: True)
    monkeypatch.setattr(collect_cb, "_ifind_amounts", lambda settings, codes: {"127110": 3.9e8})
    cov = [cov_row(), cov_row(code="113042", **{"债券简称": "上银转债", "债现价": "115.328"})]

    spot, source = collect_cb._load_spot(BrokenSina(), None, cov, DAY)

    assert source == collect_cb.SOURCE_EM_IFIND
    assert {row["code"] for row in spot} == {"127110", "113042"}
    assert next(row for row in spot if row["code"] == "127110")["amount"] == pytest.approx(3.9e8)
    # 上银没有成交额兜底值，就留 None
    assert next(row for row in spot if row["code"] == "113042")["amount"] is None


def test_load_spot_raises_when_both_sources_empty(monkeypatch):
    class BrokenSina:
        def cb_spot(self):
            return []

    monkeypatch.setattr(collect_cb, "MIN_SPOT_ROWS", 1)
    monkeypatch.setattr(collect_cb, "_recent_universe", lambda: {"127110"})

    with pytest.raises(RuntimeError, match="东财兜底也只有"):
        collect_cb._load_spot(BrokenSina(), None, [], DAY)


def test_load_spot_raises_without_previous_universe(monkeypatch):
    """库里一次都没采过时不能兜底 —— 没有「今天该有谁」的基准，宁可报错。"""

    class BrokenSina:
        def cb_spot(self):
            return []

    monkeypatch.setattr(collect_cb, "MIN_SPOT_ROWS", 1)
    monkeypatch.setattr(collect_cb, "_recent_universe", lambda: set())

    with pytest.raises(RuntimeError, match="无法兜底"):
        collect_cb._load_spot(BrokenSina(), None, [cov_row()], DAY)


def test_merge_daily_records_fallback_source():
    spot = [spot_row()]
    rows = collect_cb._merge_daily(
        spot,
        {"127110": cov_row()},
        {"127110": redeem_row()},
        {"127110": CbBasic(bond_code="127110", list_date=date(2025, 7, 25))},
        DAY,
        collect_cb.SOURCE_EM_IFIND,
    )

    assert rows[0]["source"] == "em+ifind"


def test_parse_redeem_days_is_strict():
    """只认开头的 `已满足/需要`；日期与超范围的一律不认（复查：宽松匹配会把日期当计数）。"""
    assert collect_cb._parse_redeem_days("12/15 | 30") == (12, 15)
    assert collect_cb._parse_redeem_days(" 15/15 | 30") == (15, 15)
    # `0/15 | 30` 是「一天都还没满足」，是有效计数（实测占绝大多数），不能当成解析失败
    assert collect_cb._parse_redeem_days("0/15 | 30") == (0, 15)
    assert collect_cb._parse_redeem_days("0/20 | 30") == (0, 20)
    # 日期不能被读成「已满足 2026 天」
    assert collect_cb._parse_redeem_days("2026/11/05") is None
    assert collect_cb._parse_redeem_days("临近到期 <span>2026/11/05 最后交易</span>") is None
    # 分子超过 30 的一律不是计数
    assert collect_cb._parse_redeem_days("99/15") is None
    assert collect_cb._parse_redeem_days("") is None
    assert collect_cb._parse_redeem_days(None) is None


def test_merge_daily_records_redeem_days_and_required():
    rows = collect_cb._merge_daily(
        [spot_row()],
        {"127110": cov_row()},
        {"127110": redeem_row()},
        {"127110": CbBasic(bond_code="127110", list_date=date(2025, 7, 25))},
        DAY,
    )

    assert rows[0]["redeem_days"] == 12
    assert rows[0]["redeem_days_required"] == 15


def test_trigger_pct_only_accepts_common_bands():
    bands = collect_cb.REDEEM_TRIGGER_PCTS
    assert collect_cb._trigger_pct(16.796, 12.92, bands) == 130.0
    assert collect_cb._trigger_pct(10.47, 8.05, bands) == 130.0
    # 算出来不像常见档位（东财触发价没跟着下修更新）就丢弃
    assert collect_cb._trigger_pct(9.0, 8.05, bands) is None
    assert collect_cb._trigger_pct(None, 8.05, bands) is None
    # 回售是 70%，不能拿强赎的档位去卡
    assert collect_cb._trigger_pct(2.51, 3.58, collect_cb.RESALE_TRIGGER_PCTS) == 70.0


def test_call_price_out_of_range_is_dropped():
    assert collect_cb.call_price_from_terms({"EXECUTE_PRICE_SH": 112}) == 112
    # 条款里的「转股价的 130%」不能被当成到期赎回价（复查 P2-6）
    assert collect_cb.call_price_from_terms({"EXECUTE_PRICE_SH": 130}) == 130
    assert collect_cb.call_price_from_terms({"REDEEM_CLAUSE": "面值的 135% 赎回"}) is None
    assert collect_cb.call_price_from_terms({"REDEEM_CLAUSE": "面值的 108% 赎回"}) == 108.0


def test_fill_terms_keeps_columns_consistent(env):
    """银行转债没有回售条款（RESALE_TRIG_PRICE='-'），不能让它那一行少个键（复查 P1-3）。"""
    _, maker, monkeypatch = env
    with maker() as session:
        session.add_all(
            [
                CbBasic(bond_code="127110", bond_name="广核转债", list_date=date(2025, 1, 1)),
                CbBasic(bond_code="113042", bond_name="上银转债", list_date=date(2025, 6, 1)),
                # `_fill_terms` 的队列按昨日名单划范围
                CbDaily(trade_date=date(2026, 10, 29), bond_code="127110", close=133.0),
                CbDaily(trade_date=date(2026, 10, 29), bond_code="113042", close=115.0),
            ]
        )
        session.commit()

    class FakeTerms:
        def cb_terms(self, code):
            if code == "113042":
                # 银行转债：没有回售条款
                return {
                    "INTEREST_RATE_EXPLAIN": "第一年0.3%、第二年0.8%",
                    "EXPIRE_DATE": "2027-01-25",
                    "TRANSFER_START_DATE": "2021-07-29",
                    "TRANSFER_PRICE": "8.05",
                    "REDEEM_TRIG_PRICE": "10.47",
                    "RESALE_TRIG_PRICE": "-",
                    "REDEEM_CLAUSE": "面值的112%赎回",
                }
            return {
                "INTEREST_RATE_EXPLAIN": "第一年0.3%、第二年0.8%",
                "EXPIRE_DATE": "2031-07-09",
                "TRANSFER_START_DATE": "2026-01-09",
                "TRANSFER_PRICE": "3.58",
                "REDEEM_TRIG_PRICE": "4.65",
                "RESALE_TRIG_PRICE": "2.51",
                "REDEEM_CLAUSE": "面值的108%赎回",
            }

    assert fill_terms_real(FakeTerms(), DAY) == 2

    with maker() as session:
        bank = session.get(CbBasic, "113042")
        assert bank.resale_trigger_pct is None
        assert bank.redeem_trigger_pct == 130.0
        assert bank.maturity_call_price == 112.0
        assert bank.terms_tried_at == DAY
        other = session.get(CbBasic, "127110")
        assert other.resale_trigger_pct == 70.0
        assert other.terms_tried_at == DAY


def test_fill_terms_marks_failures_so_queue_does_not_stall(env):
    """补不上的券也要写 `terms_tried_at`，否则明天还是它占着队头（复查 P1-2）。"""
    _, maker, monkeypatch = env
    with maker() as session:
        session.add(CbBasic(bond_code="127110", bond_name="广核转债", list_date=date(2025, 1, 1)))
        session.add(CbDaily(trade_date=date(2026, 10, 29), bond_code="127110", close=133.0))
        session.commit()

    class Failing:
        def cb_terms(self, code):
            return {}

    assert fill_terms_real(Failing(), DAY) == 0
    with maker() as session:
        row = session.get(CbBasic, "127110")
        assert row.terms_tried_at == DAY
        assert row.coupon_text is None


def test_mark_delisted_handles_redeem_delisting(env):
    """强赎摘牌的券到期日还没到，也要按最后交易日写 delist_date（复查 P2-5）。"""
    _, maker, _ = env
    with maker() as session:
        session.add(CbBasic(bond_code="127025", bond_name="Z东转债"))
        session.add(
            CbDaily(
                trade_date=date(2026, 10, 29),
                bond_code="127025",
                close=105.9,
                last_trade_date=date(2026, 10, 30),
            )
        )
        session.commit()

    assert collect_cb._mark_delisted(DAY, set()) == 1
    with maker() as session:
        assert session.get(CbBasic, "127025").delist_date == date(2026, 10, 30)


def test_ifind_fallback_respects_quota_and_dedup(env):
    _, maker, monkeypatch = env
    # 与 DDE 补齐同一个门槛：DDE 都让路了，成交额兜底也不该花
    monkeypatch.setattr(
        collect_cb, "quota_level", lambda settings=None: collect_cb.QuotaLevel.PAUSE_HIT_DDE
    )
    assert collect_cb._ifind_allowed(None, DAY) is False
    monkeypatch.setattr(
        collect_cb, "quota_level", lambda settings=None: collect_cb.QuotaLevel.CORE_ONLY
    )
    assert collect_cb._ifind_allowed(None, DAY) is False

    monkeypatch.setattr(
        collect_cb, "quota_level", lambda settings=None: collect_cb.QuotaLevel.NORMAL
    )
    assert collect_cb._ifind_allowed(None, DAY) is True
    with maker() as session:
        session.add(
            CbDaily(trade_date=DAY, bond_code="127110", close=133.0, source="em+ifind")
        )
        session.commit()
    assert collect_cb._ifind_allowed(None, DAY) is False


def test_refresh_refuses_non_today(env):
    _, _, _ = env
    with pytest.raises(RuntimeError, match="只能当天采"):
        collect_cb.refresh(trade_date=date(2020, 1, 2))


def test_refresh_raises_when_coverage_drops(env):
    """昨天 10 只、今天只给 5 只（<80%）→ 抛错且今天一行都不写。"""
    _, maker, monkeypatch = env
    with maker() as session:
        session.add_all(
            [
                CbDaily(trade_date=date(2026, 10, 29), bond_code=f"12711{index}", close=110.0)
                for index in range(10)
            ]
        )
        session.commit()

    install(
        monkeypatch,
        [spot_row(code=f"12711{index}") for index in range(5)],
        [cov_row(code=f"12711{index}") for index in range(5)],
        [redeem_row(code=f"12711{index}") for index in range(5)],
    )
    # 行情只取到 5 只，先撞 MIN_SPOT_ROWS 之前把阈值调低，专测覆盖率闸
    monkeypatch.setattr(collect_cb, "MIN_SPOT_ROWS", 1)

    with pytest.raises(RuntimeError, match="覆盖率不足"):
        collect_cb.refresh(trade_date=DAY)

    with maker() as session:
        assert session.scalars(select(CbDaily).where(CbDaily.trade_date == DAY)).all() == []


def test_refresh_marks_delisted_but_keeps_basic(env):
    """已到期、且今天行情里没有的券：写 delist_date，不删 basic 行。"""
    _, maker, monkeypatch = env
    with maker() as session:
        session.add_all(
            [
                CbBasic(
                    bond_code="110001",
                    bond_name="已到期转债",
                    maturity_date=date(2026, 10, 20),
                    list_date=date(2020, 1, 1),
                ),
                CbBasic(bond_code="127110", bond_name="广核转债"),
                # 上一采集日的数据（覆盖率闸的基准）
                *[
                    CbDaily(trade_date=date(2026, 10, 29), bond_code="127110", close=133.0)
                    for _ in range(1)
                ],
            ]
        )
        session.commit()

    install(monkeypatch, [spot_row()], [cov_row()], [redeem_row()])
    monkeypatch.setattr(collect_cb, "MIN_SPOT_ROWS", 1)

    written = collect_cb.refresh(trade_date=DAY)

    assert written == 1
    with maker() as session:
        gone = session.get(CbBasic, "110001")
        assert gone is not None
        assert gone.delist_date == date(2026, 10, 20)
        assert session.get(CbBasic, "127110") is not None
        assert session.scalars(select(CbDaily).where(CbDaily.trade_date == DAY)).all()

def test_rerun_without_amounts_keeps_earlier_ifind_values(env, monkeypatch):
    """新浪挂一整天时，19:30 的全量重跑不能把 17:30 兜到的成交额抹掉（复查 P1）。

    原来的 `upsert_many` 会把这一趟的 `amount=None` / `turnover=None` 直接刷进库，
    而 `t0` 预设要求成交额 ≥5000 万 —— 19:30 之后整池就变空了。
    """
    _, maker, _ = env
    with maker() as session:
        session.add(
            CbBasic(
                bond_code="127110",
                bond_name="广核转债",
                stock_code="003816",
                stock_name="中国广核",
                list_date=date(2025, 7, 25),
            )
        )
        # 昨日名单：兜底要拿它划「今天该有行情的券」
        session.add(CbDaily(trade_date=date(2026, 10, 29), bond_code="127110", close=133.0))
        # 今天 17:30 那一趟：价格来自东财、成交额来自 iFinD
        session.add(
            CbDaily(
                trade_date=DAY,
                bond_code="127110",
                close=133.11,
                amount=3.9e8,
                turnover=0.8,
                source=collect_cb.SOURCE_EM_IFIND,
            )
        )
        session.commit()

    # 新浪仍然挂着（spot 为空 → 走东财兜底），iFinD 今天已经取过 → 不再取
    install(monkeypatch, [], [cov_row()], [redeem_row()])
    monkeypatch.setattr(collect_cb, "MIN_SPOT_ROWS", 1)
    monkeypatch.setattr(collect_cb, "_ifind_allowed", lambda settings, day: False)

    assert collect_cb.refresh(trade_date=DAY) == 1

    with maker() as session:
        row = session.get(CbDaily, (DAY, "127110"))
        assert row.amount == pytest.approx(3.9e8), "成交额被重跑抹掉了"
        assert row.turnover == pytest.approx(0.8)
        assert row.close == pytest.approx(133.11)
        # 库里成交额是 iFinD 兜来的，source 就该继续标 em+ifind
        assert row.source == collect_cb.SOURCE_EM_IFIND


def test_load_spot_labels_source_by_amount_presence(env, monkeypatch):
    """没有成交额 → source=em；今天早先已兜过 → 仍标 em+ifind（与库里的数据一致）。"""
    _, maker, _ = env
    source = FakeSource([], [cov_row()], [redeem_row()])
    monkeypatch.setattr(collect_cb, "_recent_universe", lambda: {"127110"})
    monkeypatch.setattr(collect_cb, "_ifind_allowed", lambda settings, day: False)
    monkeypatch.setattr(collect_cb, "MIN_SPOT_ROWS", 1)

    _, label = collect_cb._load_spot(source, None, [cov_row()], DAY)
    assert label == collect_cb.SOURCE_EM

    with maker() as session:
        session.add(
            CbDaily(
                trade_date=DAY,
                bond_code="127110",
                close=133.11,
                amount=3.9e8,
                source=collect_cb.SOURCE_EM_IFIND,
            )
        )
        session.commit()

    _, label = collect_cb._load_spot(source, None, [cov_row()], DAY)
    assert label == collect_cb.SOURCE_EM_IFIND
