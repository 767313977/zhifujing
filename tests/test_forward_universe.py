"""B1 证券清单测试：哈希稳定、北交所默认不可前瞻、不按代码段放行。

全部用临时库，不读真实 `history.db`。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts import forward_store as store  # noqa: E402
from scripts import forward_universe as universe  # noqa: E402

START = date(2024, 1, 1)
INGESTED = "2026-11-12T15:40:00+08:00"


def _build(path: Path, codes: list[str], days: int = 5) -> None:
    engine = store.connect(path)
    store.create_schema(engine)
    with engine.begin() as conn:
        for code in codes:
            for offset in range(days):
                day = (START + timedelta(days=offset)).isoformat()
                conn.exec_driver_sql(
                    """
                    INSERT INTO stock_daily
                    (trade_date, code, name, open, high, low, close, pct_chg,
                     volume, amount, turnover, board, source, pct_source,
                     raw_code, data_version, ingested_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        day, code, f"测试{code}", 10.0, 10.2, 9.8, 10.0, 0.5,
                        1e6, 1e7, 0.5, store.board_of(code), "test", "official",
                        f"{code}.SH", "v1", INGESTED,
                    ),
                )
    engine.dispose()


@pytest.fixture()
def manifest(tmp_path):
    db = tmp_path / "f.db"
    _build(db, ["600000", "000001", "830001", "920001"])
    engine = store.connect_readonly(db)
    try:
        with Session(engine) as session:
            data = universe.build_manifest(
                session, source_db=str(db), generated_at=INGESTED
            )
    finally:
        engine.dispose()
    return data


def test_manifest_counts_and_cutoff(manifest):
    assert manifest["data_cutoff"] == (START + timedelta(days=4)).isoformat()
    assert manifest["counts"]["total"] == 4
    # 830001 / 920001 都是北交所 → 默认不可前瞻
    assert manifest["counts"]["forward_eligible"] == 2
    assert manifest["counts"]["excluded_unverified"] == 2
    assert manifest["counts"]["by_board"] == {"沪主板": 1, "深主板": 1, "北交所": 2}


def test_bse_is_not_auto_allowed(manifest):
    by_code = {item["code"]: item for item in manifest["securities"]}
    for code in ("830001", "920001"):
        assert by_code[code]["board"] == "北交所"
        assert by_code[code]["identity_verified"] is False
        assert by_code[code]["forward_eligible"] is False
    assert by_code["600000"]["forward_eligible"] is True


def test_manifest_hash_is_stable(manifest, tmp_path):
    db = tmp_path / "f.db"
    engine = store.connect_readonly(db)
    try:
        with Session(engine) as session:
            again = universe.build_manifest(
                session, source_db=str(db), generated_at="2026-11-13T09:00:00+08:00"
            )
    finally:
        engine.dispose()
    assert again["universe_sha256"] == manifest["universe_sha256"]
    assert again["securities"] == manifest["securities"]


def test_hash_changes_when_universe_changes(tmp_path, manifest):
    db2 = tmp_path / "g.db"
    _build(db2, ["600000", "000001", "830001", "920001", "600001"])
    engine = store.connect_readonly(db2)
    try:
        with Session(engine) as session:
            other = universe.build_manifest(
                session, source_db=str(db2), generated_at=INGESTED
            )
    finally:
        engine.dispose()
    assert other["universe_sha256"] != manifest["universe_sha256"]


def test_eligible_codes_excludes_unverified(manifest):
    codes = universe.eligible_codes(manifest)
    assert sorted(codes) == ["000001", "600000"]


def test_manifest_does_not_touch_readonly_source(manifest, tmp_path):
    """生成清单只读：不应改变源库内容。"""
    db = tmp_path / "f.db"
    before = db.stat().st_size
    engine = store.connect_readonly(db)
    try:
        with Session(engine) as session:
            universe.build_manifest(session, source_db=str(db), generated_at=INGESTED)
    finally:
        engine.dispose()
    assert db.stat().st_size == before
