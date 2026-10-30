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
from app.models import Base, CbBasic, CbDaily

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
    monkeypatch.setattr(collect_cb, "_fill_terms", lambda source: 0)
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
