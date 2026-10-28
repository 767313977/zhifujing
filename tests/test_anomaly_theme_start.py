"""「题材启动日」的判定（api/anomaly_map._start_days / _theme_scope / _theme_counts）。

2026-10-28 改了两处口径，这里逐条钉住：

1. **统计范围放宽到创业板 / 科创板**：原先按表格的展示过滤（主板 + 非ST + 总市值≥40亿）
   统计，20cm 的票完全不参与计数 —— 而题材往往正是被 300/688 带起来的。
   现在只剔 ST 与北交所。
2. **启动日是「本轮」的第一次，不是窗口内第一次**：往回看 120 个交易日，相邻两次达标
   相隔 10 个交易日以上就断代；否则窗口首日活跃的板块会被误标成当天启动。
"""

from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.anomaly_map import (
    _big_yang_pct,
    _is_display_board,
    _min_total_mv,
    _start_days,
    _theme_counts,
    _theme_scope,
)
from app.models import LimitPool, StockBasic, StockDaily


def test_display_board_includes_20cm():
    """展示范围 2026-10-28 放开：创业板 / 科创板要进表（原先只有主板）。"""
    assert _is_display_board("600000")
    assert _is_display_board("300001")
    assert _is_display_board("301001")
    assert _is_display_board("688001")


def test_display_board_excludes_bse_and_b_share():
    assert not _is_display_board("830799")  # 北交所
    assert not _is_display_board("920001")  # 北交所新段
    assert not _is_display_board("900901")  # 沪 B
    assert not _is_display_board("200002")  # 深 B


def test_big_yang_threshold_is_double_for_20cm():
    """20cm 的「中大阳线」门槛是 10%，主板仍是 5% —— 否则一天几百只淹掉整张表。"""
    assert _big_yang_pct("600000") == 5.0
    assert _big_yang_pct("300001") == 10.0
    assert _big_yang_pct("688001") == 10.0


def test_market_cap_floor_is_lower_for_20cm():
    """市值门槛：20cm 20 亿、主板 40 亿（2026-10-28 用户要求）。"""
    assert _min_total_mv("600000") == 40e8
    assert _min_total_mv("300001") == 20e8
    assert _min_total_mv("688001") == 20e8


def _calendar(n: int) -> list[date]:
    """合成一段「交易日」——`_start_days` 只按传入的列表算间隔，不查日历。"""
    return [date(2026, 9, 1) + timedelta(days=i) for i in range(n)]


def test_scope_includes_chinext_and_star():
    """20cm 的创业板 / 科创板必须计入（这是本次改动的核心）。"""
    assert _theme_scope("300001", "特锐德")
    assert _theme_scope("688001", "华兴源创")
    assert _theme_scope("600000", "浦发银行")


def test_scope_excludes_st_and_bse():
    assert not _theme_scope("600000", "ST浦发")
    assert not _theme_scope("600000", "*ST某某")
    assert not _theme_scope("830799", "艾融软件")  # 北交所 8 开头
    assert not _theme_scope("430047", "诺思兰德")  # 北交所 4 开头
    assert not _theme_scope("920001", "北交所新股")  # 920 新段


def test_run_breaks_when_gap_exceeds_threshold():
    """两段相隔超过 10 个交易日 → 只认后一段的起点。"""
    days = _calendar(30)
    counts = {}
    for index in (0, 1):  # 上一轮
        counts[(days[index], "半导体")] = [2, 2]
    for index in (20, 21):  # 本轮
        counts[(days[index], "半导体")] = [2, 2]

    assert _start_days(counts, days)["半导体"] == days[20]


def test_run_merges_when_gap_within_threshold():
    """相隔 5 个交易日仍算同一轮 → 起点往前推。"""
    days = _calendar(12)
    counts = {(days[0], "机器人"): [2, 2], (days[5], "机器人"): [2, 2]}

    assert _start_days(counts, days)["机器人"] == days[0]


def test_threshold_needs_two_limit_ups_or_one_plus_three():
    """判据：涨停 ≥2，或 涨停 ≥1 且异动 ≥3。差一条就不算达标。"""
    days = _calendar(4)
    counts = {
        (days[0], "不达标A"): [1, 2],  # 涨停 1、异动 2 → 不达标
        (days[1], "达标B"): [1, 3],  # 涨停 1、异动 3 → 达标
        (days[2], "达标C"): [2, 2],  # 涨停 2 → 达标
        (days[3], "不达标D"): [0, 5],  # 一只没涨停，只有大阳 → 不达标
    }
    starts = _start_days(counts, days)

    assert "不达标A" not in starts
    assert "不达标D" not in starts
    assert starts["达标B"] == days[1]
    assert starts["达标C"] == days[2]


def _session(tmp_path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'theme.db'}")
    for model in (LimitPool, StockBasic, StockDaily):
        model.__table__.create(engine)
    return Session(engine)


def test_counts_include_chinext_limit_ups(tmp_path):
    """创业板的两只涨停就该让板块达标 —— 旧口径（只要主板）会漏掉这一天。"""
    day = date(2026, 10, 28)
    with _session(tmp_path) as db:
        db.add_all(
            [
                StockBasic(code="300001", name="特锐德", industry="电力设备-电网设备-线缆"),
                StockBasic(code="300002", name="神州泰岳", industry="电力设备-电网设备-线缆"),
                StockBasic(code="600000", name="浦发银行", industry="银行-银行-股份制银行"),
            ]
        )
        db.add_all(
            [
                LimitPool(trade_date=day, code="300001", pool_type="up", name="特锐德"),
                LimitPool(trade_date=day, code="300002", pool_type="up", name="神州泰岳"),
                LimitPool(trade_date=day, code="600000", pool_type="up", name="ST浦发"),
            ]
        )
        db.commit()

        counts, dates = _theme_counts(db, day)

    assert dates == [day]
    # 线缆两只都算（创业板），银行那只名字带 ST 被剔
    assert counts[(day, "线缆")] == [2, 2]
    assert (day, "股份制银行") not in counts or counts[(day, "股份制银行")][0] == 0
