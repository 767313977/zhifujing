"""ETF 次日验证与轮动回测（ETF-4）的四条口径。

1. **次日收盘起算**：信号日收盘出名单，收益从 `t+1` 收盘算到 `t+1+hold` 收盘
   （不是信号日收盘 —— 那个价根本买不到）；
2. **已退市 / 停牌的 ETF 留在样本里**：持有期内没有日线就按它最后一份结算；
3. **没有未来函数**：同指数代表按**当时**的 20 日均额选（今天谁活跃不影响昨天）；
4. **`inflow` 依赖份额**：没有份额历史就选不出标的，写进 `note`（不拿 0 顶）。
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, EtfBar, EtfBasic, EtfCategory, EtfShare
from app.services import etf_screen, etf_track

END = date(2026, 10, 30)
DAYS = 100


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'etf-track.db'}")
    Base.metadata.create_all(engine)
    # 数据集有进程内缓存（键里有引擎），每个测试换库时清一次
    etf_track._DATA_CACHE = None
    with Session(engine) as session:
        yield session


def _dates(size: int = DAYS) -> list[date]:
    days: list[date] = []
    cursor = END
    while len(days) < size:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def seed(
    db,
    code,
    name,
    norm,
    fund_type="股票",
    category="宽基",
    step=1.0,
    amount=5e8,
    shares=1e10,
    bars=DAYS,
    share_days=0,
):
    db.add(EtfBasic(code=code, name=name, norm_name=norm, fund_type=fund_type, t0=False))
    db.add(EtfCategory(code=code, name=name, category=category))
    days = _dates()[:bars]
    close = 1.0
    for day in days:
        prev = close
        close = round(close * (1 + step / 100), 6)
        db.add(
            EtfBar(
                trade_date=day,
                code=code,
                close=close,
                prev_close=prev,
                pct_chg=step,
                amount=amount,
                volume=1000,
                source="tdx",
            )
        )
    for day in _dates()[-share_days:] if share_days else []:
        db.add(EtfShare(trade_date=day, code=code, shares=shares, close=close))


def test_forward_return_starts_at_next_close(db):
    """信号日 t、持有 5 天 → 用 `1.01^5`，不是 `1.01^6`（后者等于信号日收盘买入）。"""
    seed(db, "510300", "沪深300ETF", "沪深300", step=1.0, share_days=25)
    db.commit()

    data = etf_track.track(db, preset="momentum", top=5, hold=5, days=20)

    assert data.rows
    first = data.rows[0]
    assert first.entry_date is not None and first.entry_date > first.trade_date
    assert first.exit_date is not None and first.exit_date > first.entry_date
    assert first.ret == pytest.approx((1.01**5 - 1) * 100, abs=0.02)
    # 基准就是它自己（只有一只），三条线应当完全一致
    assert first.hs300_ret == pytest.approx(first.ret, abs=0.02)
    assert first.universe_ret == pytest.approx(first.ret, abs=0.02)


def test_delisted_settles_on_last_bar(db):
    """退市 / 停牌后没有日线：入场取它下一份、出场取最后一份（样本不剔除）。"""
    seed(db, "510300", "沪深300ETF", "沪深300", step=1.0, share_days=25)
    seed(db, "159999", "退市ETF", "退市", step=2.0, bars=90, share_days=25)
    db.commit()

    days = _dates()
    data = etf_track.track(db, preset="momentum", top=2, hold=5, days=40)

    row = next(item for item in data.rows if item.trade_date == days[84])
    picks = {pick.code: pick.ret for pick in row.picks}
    # 85 入场、最后一根是 89 → 实际只持有了 4 个交易日
    assert picks["159999"] == pytest.approx((1.02**4 - 1) * 100, abs=0.02)
    assert picks["510300"] == pytest.approx((1.01**5 - 1) * 100, abs=0.02)


def test_dedup_uses_asof_liquidity(db):
    """去重代表按**当时**的 20 日均额选：不能拿今天的成交额倒推昨天。"""
    days = _dates()
    for code, early, late in (("510300", 2e8, 1e6), ("510310", 1e6, 2e8)):
        db.add(EtfBasic(code=code, name=f"{code}ETF", norm_name="沪深300", fund_type="股票"))
        db.add(EtfCategory(code=code, name=f"{code}ETF", category="宽基"))
        close = 1.0
        for index, day in enumerate(days):
            prev = close
            close = close * 1.01
            db.add(
                EtfBar(
                    trade_date=day,
                    code=code,
                    close=close,
                    prev_close=prev,
                    pct_chg=1.0,
                    amount=early if index < 80 else late,
                    volume=1000,
                    source="tdx",
                )
            )
        for day in days:
            db.add(EtfShare(trade_date=day, code=code, shares=1e10, close=close))
    db.commit()

    dataset = etf_track._dataset(db)
    assert dataset is not None
    early = etf_screen.select_rows(
        etf_screen.to_rows(etf_track._raws_at(dataset, 70), dedup=True), "momentum", 5
    )
    recent = etf_screen.select_rows(
        etf_screen.to_rows(etf_track._raws_at(dataset, 95), dedup=True), "momentum", 5
    )

    assert [row.code for row in early] == ["510300"]
    assert [row.code for row in recent] == ["510310"]


def test_inflow_needs_share_history(db):
    """份额从 2026-09-21 才有：没有份额就选不出资金流入，写明原因而不是给空表。"""
    seed(db, "512000", "券商ETF", "券商", step=-0.5)
    db.commit()

    data = etf_track.track(db, preset="inflow", top=5, hold=5, days=20)

    assert data.rows == []
    assert data.note and "份额" in data.note
    dataset = etf_track._dataset(db)
    assert dataset is not None
    raws = etf_track._raws_at(dataset, len(dataset.days) - 6)
    assert all(raw.flow20 is None and raw.flow5 is None for raw in raws)


def test_rotation_charges_turnover_once(db):
    """成本单边万 0.5：首次建仓付一次，之后同一只不动就不该再收费。"""
    seed(db, "510300", "沪深300ETF", "沪深300", step=1.0, share_days=25)
    db.commit()

    result = etf_track.rotation(db, preset="momentum", top=5, step=5, days=60)

    assert result.periods
    first = result.periods[0]
    # 期收益 = 1.01^5 − 1；首次建仓换手 1 → 扣 0.005%
    assert first["gross"] == pytest.approx((1.01**5 - 1) * 100, abs=0.02)
    assert first["cost"] == pytest.approx(0.005, abs=0.001)
    assert first["ret"] == pytest.approx(first["gross"] - 0.005, abs=0.01)
    # 同一只 ETF 一直持有 → 没有换手
    assert result.periods[1]["cost"] == pytest.approx(0.0, abs=1e-9)


def test_api_track_shape(db):
    """接口层的字段映射：逐日超额、统计与曲线都要给齐（页面直接拿来渲染）。"""
    from app.api import etf as api_etf

    api_etf._track_cache.clear()
    seed(db, "510300", "沪深300ETF", "沪深300", step=1.0, share_days=25)
    db.commit()

    out = api_etf.track(preset="momentum", top=5, hold=5, days=20, session=db)

    assert out.preset == "momentum"
    assert out.rows
    first = out.rows[0]
    assert first.entry_date is not None and first.entry_date > first.trade_date
    assert first.excess_universe is not None
    assert out.stats.strategy.samples == len(out.rows)
    assert out.curve and out.curve[-1].strategy is not None
