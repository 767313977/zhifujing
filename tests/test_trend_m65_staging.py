"""M6.5 staging 护栏测试：只追加、来源隔离、PIT 语义。"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.trend_engine import multisource as ms  # noqa: E402

NOW = "2026-11-15T00:00:00+00:00"


def _batch(tmp_path: Path, batch_id: str = "batch_test") -> ms.StagingBatch:
    return ms.StagingBatch(tmp_path / "m65_staging", batch_id, now=NOW)


def test_batch_directory_is_unique(tmp_path: Path) -> None:
    batch = _batch(tmp_path)
    batch.close()
    with pytest.raises(ms.StagingViolation):
        _batch(tmp_path)


def test_raw_payload_append_only_and_provider_isolated(tmp_path: Path) -> None:
    batch = _batch(tmp_path)
    payload = {"data": [{"code": "600519", "ex_date": "2026-06-26"}]}
    batch.stage_raw("Tushare", "dividend", payload)
    batch.stage_raw("Lixinger", "cn/company/dividend", payload)
    assert batch.counts()["raw_payload_manifest"] == 2
    with pytest.raises(sqlite3.IntegrityError):
        batch.stage_raw("Tushare", "dividend", payload)
    batch.close()


def test_multi_provider_same_event_not_overwritten(tmp_path: Path) -> None:
    batch = _batch(tmp_path)
    row = {
        "code": "600519",
        "ex_date": "2026-06-26",
        "record_date": "2026-06-25",
        "cash_per_share_pretax": 23.0,
        "license_scope": "INTERNAL_RESEARCH",
    }
    batch.stage_corporate_action({"provider": "Tushare", "endpoint": "dividend", **row})
    batch.stage_corporate_action({"provider": "Lixinger", "endpoint": "cn/company/dividend", **row})
    rows = batch.connection.execute(
        "SELECT provider, cash_per_share_pretax FROM corporate_action_raw ORDER BY provider"
    ).fetchall()
    assert rows == [("Lixinger", 23.0), ("Tushare", 23.0)]
    batch.close()


def test_available_at_missing_stays_null(tmp_path: Path) -> None:
    batch = _batch(tmp_path)
    batch.stage_factor(
        {"provider": "Tushare", "code": "600519", "trade_date": "2026-06-26", "factor": 8.88, "available_at": ""}
    )
    stored = batch.connection.execute("SELECT available_at FROM adjustment_factor_raw").fetchone()[0]
    assert stored is None
    batch.close()


def test_announcement_date_is_never_promoted_to_timestamp() -> None:
    assert ms.normalize_announcement("2026-06-26") == ("2026-06-26", "date")
    assert ms.normalize_announcement("20260626") == ("2026-06-26", "date")
    assert ms.normalize_announcement("2026-06-26T00:00:00+08:00") == ("2026-06-26", "date")
    assert ms.normalize_announcement(None) == (None, "none")
    assert ms.normalize_announcement("") == (None, "none")
    with pytest.raises(ValueError):
        ms.normalize_announcement("2026/06/26")


def test_snapshot_only_cannot_carry_interval() -> None:
    with pytest.raises(ms.StagingViolation):
        ms.assert_snapshot_mode(ms.SNAPSHOT_ONLY, "2025-03-05", None)
    with pytest.raises(ms.StagingViolation):
        ms.assert_snapshot_mode(ms.INTERVAL, None, "2026-01-05")
    ms.assert_snapshot_mode(ms.SNAPSHOT_ONLY, None, None)
    ms.assert_snapshot_mode(ms.INTERVAL, "2025-03-05", None)


def test_snapshot_cannot_backfill_from_current(tmp_path: Path) -> None:
    with pytest.raises(ms.StagingViolation):
        ms.assert_snapshot_not_from_current("2025-03-05", None)
    with pytest.raises(ms.StagingViolation):
        ms.assert_snapshot_not_from_current("2025-03-05", "2026-09-28")
    ms.assert_snapshot_not_from_current("2025-03-05", "2025-03-05")
    batch = _batch(tmp_path)
    with pytest.raises(ms.StagingViolation):
        batch.stage_snapshot(
            {
                "provider": "Tushare",
                "provider_sector_id": "BK1184.DC",
                "snapshot_date": "2025-03-05",
                "constituent_code": "002117.SZ",
                "membership_mode": ms.SNAPSHOT_ONLY,
                "provider_asof": None,
            }
        )
    batch.close()


def test_fuzzy_sector_mapping_requires_review() -> None:
    fuzzy = ms.make_mapping(
        provider="Tushare",
        b1_sector_id="801001",
        provider_sector_id="BK1036.DC",
        mapping_method="name_fuzzy_candidate",
        confidence=0.4,
    )
    assert fuzzy["review_status"] == "pending"
    with pytest.raises(ms.MappingNotApproved):
        ms.require_approved_mapping(fuzzy)

    official = ms.make_mapping(
        provider="Tushare",
        b1_sector_id="881120",
        provider_sector_id="801730.SI",
        mapping_method="official_code_table",
        confidence=1.0,
    )
    assert official["review_status"] == "approved"
    ms.require_approved_mapping(official)

    with pytest.raises(ValueError):
        ms.make_mapping(
            provider="Tushare",
            b1_sector_id="801001",
            provider_sector_id="X",
            mapping_method="guess",
            confidence=0.5,
        )


def test_failed_batch_does_not_affect_other_batches(tmp_path: Path) -> None:
    first = _batch(tmp_path, "batch_a")
    first.stage_raw("Tushare", "dividend", {"data": [1]})
    first.close()

    with pytest.raises(ms.StagingViolation):
        _batch(tmp_path, "batch_a")

    second = _batch(tmp_path, "batch_b")
    assert second.counts()["raw_payload_manifest"] == 0
    second.close()
    assert (tmp_path / "m65_staging" / "batch_a" / "m65_multisource_staging.db").exists()


def test_staging_never_targets_research_dbs() -> None:
    for name in ("b1_research.db", "history.db", "b2_final_snapshot.db", "fupan.db"):
        with pytest.raises(ms.StagingViolation):
            ms.assert_not_research_db(Path("backend/data") / name)
    ms.assert_not_research_db(Path("backend/data/m65_staging/batch_x/m65_multisource_staging.db"))


def test_ingestion_error_recorded_not_swallowed(tmp_path: Path) -> None:
    batch = _batch(tmp_path)
    batch.stage_error("Tushare", "dc_member", "PERMISSION_DENIED", "6000 积分不足")
    row = batch.connection.execute("SELECT error_code, detail FROM ingestion_error").fetchone()
    assert row[0] == "PERMISSION_DENIED"
    batch.close()


def test_batch_id_validation(tmp_path: Path) -> None:
    with pytest.raises(ms.StagingViolation):
        ms.StagingBatch(tmp_path, "../escape", now=NOW)
