"""M6.3 缓存身份测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.trend_engine.cache_identity import (  # noqa: E402
    CacheIdentityError,
    build_cache_key,
)

BASE = {
    "db_sha256": "A93BC080EB9E366D08025C0A247E9AF525674A0294EC4D6012B7DBFD8941C56B",
    "code_sha": "abc123",
    "params": {"top": 20, "horizon": 20},
    "data_version": "b1_20261009",
}


def test_cache_key_is_deterministic() -> None:
    first = build_cache_key(**BASE)
    second = build_cache_key(**BASE)
    assert first == second
    assert first.startswith("m63:")


@pytest.mark.parametrize(
    "field,value",
    [
        ("db_sha256", "B93BC080EB9E366D08025C0A247E9AF525674A0294EC4D6012B7DBFD8941C56B"),
        ("code_sha", "def456"),
        ("data_version", "b1_20261010"),
        ("params", {"top": 10, "horizon": 20}),
    ],
)
def test_cache_key_changes_with_identity(field: str, value) -> None:
    changed = {**BASE, field: value}
    assert build_cache_key(**changed) != build_cache_key(**BASE)


@pytest.mark.parametrize("field", ["db_sha256", "code_sha", "data_version"])
def test_cache_key_rejects_empty_identity(field: str) -> None:
    with pytest.raises(CacheIdentityError):
        build_cache_key(**{**BASE, field: ""})


def test_cache_key_rejects_empty_params() -> None:
    with pytest.raises(CacheIdentityError):
        build_cache_key(**{**BASE, "params": {}})
