"""可转债与个股联动（可转债-5）。

钉这几件事（2026-10-30 复查 P1-1 / P1-2 / P2-1 / P2-2 / P2-3）：

1. 个股页「存续转债」只列**最新快照里还有行情**的券（已摘牌、老券都不能挂上去）；
2. 自选可以放转债代码：名称取 `cb_basic`、行情取 `cb_daily`（兜底日没有涨跌幅也要显示）；
3. 凡按「股票口径」处理自选的地方都要剔掉转债（每日同步 / 预算 / 计划）；
4. 转债强赎提醒走**单独推送**：按券去重，快照没采到时不发。
"""

from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api import watchlist as watchlist_api
from app.jobs import collect_daily, push_brief
from app.models import (
    AppUser,
    Base,
    CbBasic,
    CbDaily,
    CollectLog,
    StockBasic,
    Watchlist,
)
from app.schemas import WatchlistIn
from app.services import brief_watchlist, convertible, watch_scope

DAY = date(2026, 10, 30)
PREV = date(2026, 10, 29)


@pytest.fixture()
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cb-link.db'}")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def db(engine):
    with Session(engine) as session:
        session.add(AppUser(username="admin", password_hash="x", is_admin=True))
        session.commit()
        yield session


@contextmanager
def _scope_for(engine):
    """把模块级 `session_scope` 指到测试库（`push_brief` / `collect_daily` 都这么用）。

    必须像真的 `session_scope` 一样**退出时提交** —— 不提交的话 `_record` 写的去重
    记录一关会话就没了，「同一只券只推一次」看起来永远不生效。
    """
    with Session(engine) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def add_bond(session, code, stock_code="601229", **kwargs):
    fields = dict(
        bond_code=code,
        bond_name=f"{code}转债",
        stock_code=stock_code,
        stock_name="上海银行",
        maturity_date=date(2027, 1, 25),
    )
    fields.update(kwargs)
    session.add(CbBasic(**fields))


def add_daily(session, day, code, **kwargs):
    fields = dict(
        trade_date=day,
        bond_code=code,
        close=118.0,
        pct_chg=1.2,
        premium_pct=8.5,
        conv_value=110.0,
        redeem_status=None,
    )
    fields.update(kwargs)
    session.add(CbDaily(**fields))


def _seed(session) -> None:
    add_bond(session, "113042")
    # 已摘牌：个股页与提醒都不该再出现
    add_bond(session, "123001", delist_date=date(2026, 10, 1))
    # 早已退市的老券：`delist_date` 没补上，靠「最新快照没有行情」排除（复查 P2-1）
    add_bond(session, "118001")
    # 别的正股的券：不该被 601229 的卡片带出来
    add_bond(session, "127999", stock_code="000001", stock_name="平安银行")
    add_daily(session, DAY, "113042", redeem_status="已公告强赎", last_trade_date=date(2026, 11, 13))
    add_daily(session, PREV, "113042", redeem_status=None)
    session.commit()


def test_bonds_of_stock_skips_delisted_and_labels_state(db):
    _seed(db)

    bonds = convertible.bonds_of_stock(db, "601229")

    # 123001（已摘牌）、118001（老券、最新快照没行情）都不出现
    assert [row.bond_code for row in bonds] == ["113042"]
    assert bonds[0].close == 118.0
    assert bonds[0].state == "已公告强赎"


def test_bonds_of_stock_empty_for_unknown_stock(db):
    _seed(db)
    assert convertible.bonds_of_stock(db, "600000") == []


def test_state_text_for_near_redeem_uses_required_days(db):
    daily = CbDaily(
        trade_date=DAY,
        bond_code="113042",
        redeem_days=12,
        redeem_days_required=15,
    )
    assert convertible._state_text(daily) == "强赎临近 12/15"


def test_watchlist_accepts_bond_code_and_reads_cb_daily(db):
    _seed(db)
    user = db.scalars(select(AppUser)).one()

    row = watchlist_api.add_watchlist(
        payload=WatchlistIn(code="113042"), user=user, session=db
    )

    assert row.code == "113042"
    assert row.name == "113042转债"
    assert row.latest_date == DAY
    assert row.close == 118.0
    assert row.pct_chg == 1.2


def test_watchlist_still_rejects_unknown_code(db):
    _seed(db)
    user = db.scalars(select(AppUser)).one()
    with pytest.raises(HTTPException):
        watchlist_api.add_watchlist(
            payload=WatchlistIn(code="999999"), user=user, session=db
        )


def test_redeem_alerts_only_announced_bonds(db):
    _seed(db)
    user = db.scalars(select(AppUser)).one()
    db.add(Watchlist(user_id=user.id, code="113042", name="113042转债"))
    # 「已满足强赎条件」不是已公告，不能提醒
    add_bond(db, "110001")
    add_daily(db, DAY, "110001", redeem_status="已满足强赎条件")
    db.commit()

    alerts = brief_watchlist.redeem_alerts(db, DAY)

    assert [alert.bond_code for alert in alerts] == ["113042"]
    line = alerts[0].line()
    assert "已公告强赎" in line
    assert "最后交易日 2026-11-13" in line
    assert "还剩 14 天" in line


def test_redeem_alerts_follows_watched_stock(db):
    """自选的是正股（不是转债代码）也要提醒 —— 持仓常常是股票。"""
    _seed(db)
    user = db.scalars(select(AppUser)).one()
    db.add(Watchlist(user_id=user.id, code="601229", name="上海银行"))
    db.commit()

    alerts = brief_watchlist.redeem_alerts(db, DAY)

    assert [alert.bond_code for alert in alerts] == ["113042"]


def test_redeem_alerts_skip_when_snapshot_missing(db):
    _seed(db)
    user = db.scalars(select(AppUser)).one()
    db.add(Watchlist(user_id=user.id, code="113042", name="113042转债"))
    db.commit()

    # 下一个交易日还没采到快照：不能拿昨天的状态冒充「今天的提醒」
    assert brief_watchlist.redeem_alerts(db, date(2026, 11, 2)) == []


def _seed_push(engine) -> None:
    """`push_cb_redeem` 的最小数据集：管理员自选里有一只今天已公告强赎的转债。"""
    with Session(engine) as session:
        user = AppUser(username="admin2", password_hash="x", is_admin=True)
        session.add(user)
        session.flush()
        session.add_all(
            [
                CbBasic(
                    bond_code="113042",
                    bond_name="113042转债",
                    stock_code="601229",
                    stock_name="上海银行",
                ),
                CbDaily(
                    trade_date=DAY,
                    bond_code="113042",
                    close=118.0,
                    redeem_status="已公告强赎",
                    last_trade_date=date(2026, 11, 13),
                ),
                Watchlist(user_id=user.id, code="113042", name="113042转债"),
            ]
        )
        session.commit()


def test_push_cb_redeem_sends_once_per_bond(engine, monkeypatch):
    """单独推送：同一天再跑不发；换一天同一只券也不重复（去重按券，不按天）。"""
    _seed_push(engine)

    sent: list[str] = []
    monkeypatch.setattr(push_brief, "session_scope", lambda: _scope_for(engine))
    monkeypatch.setattr(
        push_brief, "send_markdown", lambda markdown, settings: sent.append(markdown) or {"ok": True}
    )
    settings = SimpleNamespace(
        feishu_push_enabled=True,
        feishu_webhook_url="https://example.invalid/hook",
        feishu_target="",
    )

    first = push_brief.push_cb_redeem(DAY, settings)
    second = push_brief.push_cb_redeem(DAY, settings)
    later = push_brief.push_cb_redeem(date(2026, 11, 2), settings)

    assert first["status"] == "ok"
    assert first["rows"] == 1
    assert "113042" in sent[0]
    assert second["status"] == "skipped"
    # 换一天也不再重复：这只券已经提醒过了
    assert later["status"] == "skipped"
    assert len(sent) == 1
    with Session(engine) as check:
        tasks = check.scalars(select(CollectLog.task)).all()
        assert tasks == ["cb_redeem:113042"]


def test_push_cb_redeem_skips_without_snapshot(engine, monkeypatch):
    _seed_push(engine)
    monkeypatch.setattr(push_brief, "session_scope", lambda: _scope_for(engine))
    sent: list[str] = []
    monkeypatch.setattr(
        push_brief, "send_markdown", lambda markdown, settings: sent.append(markdown) or {"ok": True}
    )
    settings = SimpleNamespace(
        feishu_push_enabled=True,
        feishu_webhook_url="https://example.invalid/hook",
        feishu_target="",
    )

    result = push_brief.push_cb_redeem(date(2026, 11, 9), settings)

    assert result["status"] == "skipped"
    assert sent == []


def test_stock_watch_codes_drop_bonds(db):
    _seed(db)
    user = db.scalars(select(AppUser)).one()
    db.add_all(
        [
            Watchlist(user_id=user.id, code="601229", name="上海银行"),
            Watchlist(user_id=user.id, code="113042", name="113042转债"),
        ]
    )
    db.add(StockBasic(code="601229", name="上海银行"))
    db.commit()

    assert watch_scope.stock_watch_codes(db) == ["601229"]
    assert watch_scope.stock_watch_codes(db, user.id) == ["601229"]
    assert watch_scope.stock_watch_count(db) == 1
    assert watch_scope.filter_stock_codes(db, ["113042", "601229"]) == ["601229"]


class _WatchStub:
    """只实现 `sync_watchlist` 用到的那几个属性，省掉 iFinD 客户端。"""

    _step_note = None

    def __init__(self, calls: list[str]):
        self.calls = calls

    def sync_stock(self, code: str, days: int = 250) -> int:
        self.calls.append(code)
        return 1


def test_sync_watchlist_never_asks_ifind_for_bonds(engine, monkeypatch):
    """每日自选同步必须把转债剔掉（复查 P1-1），否则天天白烧 iFinD 配额。"""
    with Session(engine) as setup:
        setup.add_all(
            [
                AppUser(username="admin3", password_hash="x", is_admin=True),
                StockBasic(code="600519", name="贵州茅台"),
                Watchlist(user_id=1, code="600519", name="贵州茅台"),
                Watchlist(user_id=1, code="113042", name="上银转债"),
            ]
        )
        setup.commit()

    calls: list[str] = []
    monkeypatch.setattr(collect_daily, "session_scope", lambda: _scope_for(engine))

    written = collect_daily.DailyCollector.sync_watchlist(_WatchStub(calls))

    assert calls == ["600519"]
    assert written == 1
    # 兜底：任何入口直接调 `sync_stock` 也要在开跑前返回 0（不查日历、不碰 iFinD）
    assert collect_daily.DailyCollector.sync_stock(_WatchStub(calls), "113042") == 0
    assert collect_daily.DailyCollector.sync_stock(_WatchStub(calls), "123001") == 0
    assert calls == ["600519"]


def test_latest_quotes_keeps_bond_row_without_pct(engine):
    """兜底日的转债只有收盘价、没有涨跌幅：不能整行跳过（复查 P2-3）。"""
    with Session(engine) as setup:
        setup.add(CbDaily(trade_date=DAY, bond_code="113042", close=118.0, pct_chg=None))
        setup.commit()

    with Session(engine) as session:
        quotes = watchlist_api._latest_quotes(session, ["113042"])

    assert quotes["113042"].trade_date == DAY
    assert quotes["113042"].close == 118.0
