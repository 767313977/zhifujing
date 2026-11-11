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


# ---- 官方证据解锁北交所 -----------------------------------------------------


def _evidence(code: str, name: str, listed: str = "2026-05-07") -> dict:
    return {
        "code": code,
        "name": name,
        "listing_date": listed,
        "switched": False,
        "old_code": None,
        "source": "https://www.bse.cn/nqxxController/nqxxCnzq.do",
    }


def test_evidence_unlocks_bse(manifest, tmp_path):
    db = tmp_path / "f.db"
    engine = store.connect_readonly(db)
    try:
        with Session(engine) as session:
            verified = universe.build_manifest(
                session,
                source_db=str(db),
                generated_at=INGESTED,
                version="b1v-test",
                bse_evidence={"920001": _evidence("920001", "测试920001")},
            )
    finally:
        engine.dispose()
    by_code = {item["code"]: item for item in verified["securities"]}
    assert by_code["920001"]["forward_eligible"] is True
    assert by_code["920001"]["official_listing_date"] == "2026-05-07"
    assert by_code["830001"]["forward_eligible"] is False  # 没有证据的仍排除
    assert verified["counts"]["bse_verified_by_evidence"] == 1
    # 证券集合没变，但可前瞻集合变了 → 两个哈希必须有一个不同
    assert verified["universe_sha256"] == manifest["universe_sha256"]
    assert verified["eligibility_sha256"] != manifest["eligibility_sha256"]


def test_evidence_name_mismatch_rejected(manifest, tmp_path):
    db = tmp_path / "f.db"
    engine = store.connect_readonly(db)
    try:
        with Session(engine) as session:
            with pytest.raises(ValueError):
                universe.build_manifest(
                    session,
                    source_db=str(db),
                    generated_at=INGESTED,
                    bse_evidence={"920001": _evidence("920001", "错误简称")},
                )
    finally:
        engine.dispose()


def test_evidence_unknown_code_rejected(manifest, tmp_path):
    db = tmp_path / "f.db"
    engine = store.connect_readonly(db)
    try:
        with Session(engine) as session:
            with pytest.raises(ValueError):
                universe.build_manifest(
                    session,
                    source_db=str(db),
                    generated_at=INGESTED,
                    bse_evidence={"999999": _evidence("999999", "不存在")},
                )
    finally:
        engine.dispose()
