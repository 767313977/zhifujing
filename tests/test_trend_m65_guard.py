"""M6.5 全局护栏测试：原始库哈希、schema 一致性、禁止合并产物。"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.trend_engine import multisource as ms  # noqa: E402

M65_DIR = ROOT / "reports/trend_engine/m65"
BASELINE = M65_DIR / "baseline_freeze.json"
PROTECTED = ("b1_research", "history", "b2_final_snapshot", "fupan")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


@pytest.mark.skipif(not BASELINE.exists(), reason="缺少 M6.5 基线冻结文件")
def test_protected_databases_unchanged() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    for name in PROTECTED:
        expected = baseline["protected_dbs"][name]["sha256"]
        path = ROOT / baseline["protected_dbs"][name]["path"]
        assert path.exists(), f"{name} 缺失"
        assert _sha256(path) == expected, f"{name} 哈希已变化，说明原始库被写入"


def test_schema_draft_matches_module() -> None:
    draft = (M65_DIR / "m65_staging_schema.sql").read_text(encoding="utf-8")
    assert ms.SCHEMA_SQL.strip() in draft
    for table in (
        "provider_manifest",
        "provider_capability",
        "raw_payload_manifest",
        "corporate_action_raw",
        "adjustment_factor_raw",
        "sector_master_raw",
        "sector_membership_snapshot",
        "sector_membership_event",
        "sector_id_crosswalk",
        "ingestion_run",
        "ingestion_error",
        "license_manifest",
    ):
        assert f"CREATE TABLE {table}" in draft


def test_no_index_staging_merge_artifacts() -> None:
    """M6.5 明确不合并指数 staging：不得出现合并脚本或审批包。"""
    forbidden = []
    for path in M65_DIR.glob("*"):
        name = path.name.lower()
        if "merge" in name or "ddl" in name.replace("schema", ""):
            forbidden.append(path.name)
    for path in (ROOT / "scripts").glob("m65_*.py"):
        text = path.read_text(encoding="utf-8").lower()
        if "insert into index_daily" in text or "attach database" in text:
            forbidden.append(path.name)
    assert forbidden == []


def test_m65_scripts_do_not_write_research_dbs() -> None:
    for path in (ROOT / "scripts").glob("m65_*.py"):
        text = path.read_text(encoding="utf-8")
        assert "INSERT INTO index_daily" not in text, path.name
        assert "DELETE FROM" not in text, path.name
        assert "mode=rw" not in text, path.name


def test_baseline_freeze_records_all_four_dbs() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert set(baseline["protected_dbs"]) == set(PROTECTED)
    assert baseline["git"]["head"]
    assert baseline["m64_reports"]
    assert baseline["m64_staging"]


def test_m65_staging_directory_is_gitignored() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "backend/data/" in gitignore


def test_default_authorized_scope_is_ifind_only() -> None:
    assert ms.DEFAULT_AUTHORIZED == frozenset({"iFinD"})
    registry = ms.ProviderRegistry()
    assert registry.allowed == frozenset({"iFinD"})
