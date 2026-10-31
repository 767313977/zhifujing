"""飞书推送订阅维度（FEAT-FEISHU-FILTER）。

钉四件事：
1. 日报总开关关掉后 `push()` 直接 skipped；
2. 形态推送的 all / high / watchlist / off 四种模式各自生效；
3. 「自选」模式取的是**管理员**的自选（简报是全局单播，没有请求上下文）；
4. 没配 webhook / 收件人时的行为与改动前一致（skipped，不抛异常）。
"""

import contextlib
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.jobs import push_brief
from app.models import AppUser, Base, PatternHit, StockBasic, Watchlist

DAY = date(2026, 10, 30)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """把 `push_brief.session_scope` 换成临时库，测试不碰线上 `fupan.db`。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'feishu.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    @contextlib.contextmanager
    def scope():
        session = maker()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(push_brief, "session_scope", scope)
    # 简报里的配额节会去读真实库，测试里没必要
    monkeypatch.setattr(push_brief, "_quota_block", lambda: "")

    with Session(engine) as session:
        admin = AppUser(username="admin", password_hash="x", is_admin=True)
        member = AppUser(username="member", password_hash="x")
        session.add_all([admin, member])
        session.flush()
        session.add_all(
            [
                StockBasic(code="300001", name="甲"),
                StockBasic(code="300002", name="乙"),
                # 管理员的自选：只有 300001
                Watchlist(user_id=admin.id, code="300001", name="甲"),
                # 会员的自选不该被读进来
                Watchlist(user_id=member.id, code="300002", name="乙"),
                # 共达模式两条命中，评分一高一低
                PatternHit(
                    trade_date=DAY,
                    code="300001",
                    pattern="limit_surge_flat",
                    name="甲",
                    score=90.0,
                    close=10.0,
                    pct_chg=1.0,
                    key_levels={"breakout": 10.5, "support": 9.5},
                    detail={},
                ),
                PatternHit(
                    trade_date=DAY,
                    code="300002",
                    pattern="limit_surge_flat",
                    name="乙",
                    score=70.0,
                    close=20.0,
                    pct_chg=2.0,
                    key_levels={"breakout": 21.0, "support": 19.0},
                    detail={},
                ),
            ]
        )
        session.commit()


def _settings(**kwargs) -> Settings:
    base = {
        "feishu_webhook_url": "https://example.invalid/hook",
        "feishu_target": "",
    }
    base.update(kwargs)
    return Settings(**base)


# ---------------------------------------------------------------- 总开关 / 缺配置


def test_daily_switch_off_skips(db):
    result = push_brief.push(DAY, _settings(feishu_daily_enabled=False))
    assert result["status"] == "skipped"
    assert "FEISHU_DAILY_ENABLED" in result["reason"]


def test_missing_channel_skips_as_before(db):
    result = push_brief.push(DAY, Settings(feishu_webhook_url="", feishu_target=""))
    assert result["status"] == "skipped"
    assert "未配置" in result["reason"]


# ------------------------------------------------------------------ 形态模式


def test_pattern_mode_off_skips(db):
    result = push_brief.push_pattern_brief(
        "limit_surge_flat", DAY, _settings(feishu_pattern_mode="off")
    )
    assert result["status"] == "skipped"
    assert "FEISHU_PATTERN_MODE" in result["reason"]


def test_pattern_mode_high_keeps_only_high_score(db):
    text = push_brief.build_pattern_brief(DAY, "limit_surge_flat", min_score=80)
    assert "300001" in text
    assert "300002" not in text


def test_pattern_mode_watchlist_keeps_only_admin_codes(db, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(
        push_brief, "send_markdown", lambda md, settings: sent.append(md) or {"ok": True}
    )
    result = push_brief.push_pattern_brief(
        "limit_surge_flat", DAY, _settings(feishu_pattern_mode="watchlist")
    )
    assert result["status"] == "ok"
    assert "300001" in sent[0]
    # 会员自选里的 300002 不该出现
    assert "300002" not in sent[0]


def test_invalid_mode_falls_back_to_all():
    assert push_brief._pattern_mode(_settings(feishu_pattern_mode="weird")) == "all"


def test_wudao_keys_are_the_same_set_as_scan_side():
    from app.jobs.scan_patterns import WUDAO_POOL_KEYS

    assert push_brief._WUDAO_KEYS == frozenset(WUDAO_POOL_KEYS)


# ------------------------------------------------------------------- 日报内容


def test_daily_brief_drops_pattern_section_when_off(db):
    text = push_brief.build_brief(
        DAY,
        _settings(feishu_pattern_mode="off", feishu_watchlist_alerts_enabled=False),
    )
    assert "形态选股" not in text


def test_daily_brief_shows_patterns_by_default(db):
    text = push_brief.build_brief(
        DAY, _settings(feishu_watchlist_alerts_enabled=False)
    )
    assert "形态选股" in text
    # 简报每形态只列前三，展示的是名称与评分
    assert "甲（90）" in text
    assert "乙（70）" in text
