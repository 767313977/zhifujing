"""期指持仓解析：只取 rank 999 合计行，按品种跨合约加总。"""

from datetime import date

import pandas as pd

from app.jobs.collect_futures_position import parse

DAY = date(2026, 10, 16)
CITIC = "中信期货(代客)"


def _frame(
    *,
    top_long: float,
    top_short: float,
    top_long_chg: float = 0.0,
    top_short_chg: float = 0.0,
    citic_long: float = 0.0,
    citic_short: float = 0.0,
    citic_long_chg: float = 0.0,
    citic_short_chg: float = 0.0,
) -> pd.DataFrame:
    """一个合约的排名表：第 1 名（多空不同家）+ 中信 + 合计行。"""
    return pd.DataFrame(
        [
            {
                "rank": 1,
                "long_party_name": "国泰君安(代客)",
                "long_open_interest": 10000.0,
                "long_open_interest_chg": 111.0,
                "short_party_name": "华泰期货(代客)",
                "short_open_interest": 9000.0,
                "short_open_interest_chg": 222.0,
            },
            {
                "rank": 2,
                "long_party_name": CITIC,
                "long_open_interest": citic_long,
                "long_open_interest_chg": citic_long_chg,
                "short_party_name": CITIC,
                "short_open_interest": citic_short,
                "short_open_interest_chg": citic_short_chg,
            },
            {
                "rank": 999,
                "long_party_name": None,
                "long_open_interest": top_long,
                "long_open_interest_chg": top_long_chg,
                "short_party_name": None,
                "short_open_interest": top_short,
                "short_open_interest_chg": top_short_chg,
            },
        ]
    )


def test_parse_uses_summary_row_not_row_sum():
    """合计行必须直接用；把 rank 1~20 再加一遍会让数值正好翻倍。"""
    tables = {
        "IF2612": _frame(
            top_long=125428,
            top_short=147096,
            top_long_chg=-7489,
            top_short_chg=-6426,
            citic_long=3000,
            citic_short=18829,
            citic_long_chg=-100,
            citic_short_chg=-50,
        )
    }
    rows = parse(DAY, tables)
    assert len(rows) == 1
    row = rows[0]
    assert row["variety"] == "IF"
    assert row["top_net_short"] == 147096 - 125428
    assert row["top_change"] == -6426 - (-7489)
    assert row["citic_net_short"] == 18829 - 3000
    assert row["citic_change"] == -50 - (-100)


def test_parse_sums_across_contracts_of_same_variety():
    tables = {
        "IF2612": _frame(top_long=1000, top_short=3000, citic_long=100, citic_short=400),
        "IF2703": _frame(top_long=500, top_short=800, citic_long=50, citic_short=150),
    }
    row = parse(DAY, tables)[0]
    assert row["top_net_short"] == (3000 - 1000) + (800 - 500)
    assert row["citic_net_short"] == (400 - 100) + (150 - 50)


def test_parse_ignores_non_index_varieties():
    tables = {"T2612": _frame(top_long=1, top_short=2)}
    assert parse(DAY, tables) == []


def test_parse_keeps_four_varieties():
    tables = {
        f"{variety}2612": _frame(top_long=1000, top_short=2000, citic_long=100, citic_short=300)
        for variety in ("IF", "IC", "IH", "IM")
    }
    rows = parse(DAY, tables)
    assert [row["variety"] for row in rows] == ["IC", "IF", "IH", "IM"]
    assert all(row["top_net_short"] == 1000 for row in rows)
