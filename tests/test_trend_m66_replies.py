"""M6.6 供应商答复登记与 PIT 语义测试。"""

from __future__ import annotations

import csv
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import m66_ifind_reply_import as imp  # noqa: E402

VENDOR_TEXT = "关于复权字段的答复：adjust_type 取值 RAW/FORWARD/BACKWARD。\n"


def _setup(tmp_path: Path):
    source = tmp_path / "reply.txt"
    source.write_text(VENDOR_TEXT, encoding="utf-8")
    return source, tmp_path / "staging", tmp_path / "register.csv"


def test_import_records_received_at_not_available_at(tmp_path: Path) -> None:
    source, staging, register = _setup(tmp_path)
    result = imp.import_reply(
        source=source,
        batch_id="m66_test",
        supplier="iFinD",
        source_channel="email",
        received_at="2026-11-16T10:00:00+08:00",
        reviewed_by="tester",
        staging_root=staging,
        register_csv=register,
    )
    assert result["status"] == "IMPORTED"
    rows = list(csv.DictReader(register.open(encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["received_at"] == "2026-11-16T10:00:00+08:00"
    assert "available_at" not in rows[0]
    assert "available_at" not in imp.REGISTER_FIELDS
    stored = ROOT / rows[0]["stored_path"] if not Path(rows[0]["stored_path"]).is_absolute() else Path(rows[0]["stored_path"])
    assert stored.exists()


def test_stored_copy_is_byte_identical(tmp_path: Path) -> None:
    source, staging, register = _setup(tmp_path)
    result = imp.import_reply(
        source=source,
        batch_id="m66_test",
        supplier="iFinD",
        source_channel="email",
        received_at="2026-11-16T10:00:00+08:00",
        reviewed_by="tester",
        staging_root=staging,
        register_csv=register,
    )
    copied = staging / "m66_test" / "ifind_reply" / f"{result['raw_sha256'][:16]}_reply.txt"
    assert copied.read_bytes() == source.read_bytes()
    assert hashlib.sha256(copied.read_bytes()).hexdigest().upper() == result["raw_sha256"]


def test_duplicate_payload_is_not_stored_twice(tmp_path: Path) -> None:
    source, staging, register = _setup(tmp_path)
    kwargs = dict(
        batch_id="m66_test",
        supplier="iFinD",
        source_channel="email",
        received_at="2026-11-16T10:00:00+08:00",
        reviewed_by="tester",
        staging_root=staging,
        register_csv=register,
    )
    first = imp.import_reply(source=source, **kwargs)
    second = imp.import_reply(source=source, **kwargs)
    assert first["status"] == "IMPORTED"
    assert second["status"] == "DUPLICATE"
    assert len(list(csv.DictReader(register.open(encoding="utf-8")))) == 1
    assert len(list((staging / "m66_test" / "ifind_reply").iterdir())) == 1


def test_sensitive_content_rejected_until_redacted(tmp_path: Path) -> None:
    source = tmp_path / "leak.txt"
    source.write_text("Authorization: Bearer abcdef1234567890\n", encoding="utf-8")
    staging, register = tmp_path / "staging", tmp_path / "register.csv"
    with pytest.raises(ValueError):
        imp.import_reply(
            source=source,
            batch_id="m66_test",
            supplier="iFinD",
            source_channel="email",
            received_at="2026-11-16T10:00:00+08:00",
            reviewed_by="tester",
            redaction_status="PENDING",
            staging_root=staging,
            register_csv=register,
        )
    result = imp.import_reply(
        source=source,
        batch_id="m66_test",
        supplier="iFinD",
        source_channel="email",
        received_at="2026-11-16T10:00:00+08:00",
        reviewed_by="tester",
        redaction_status="DONE",
        staging_root=staging,
        register_csv=register,
    )
    assert result["status"] == "IMPORTED"


def test_question_status_enum_and_append(tmp_path: Path) -> None:
    question_csv = tmp_path / "questions.csv"
    row = imp.append_question_status(
        question_id="Q-A1",
        status="ANSWERED",
        evidence_ref="reply:ABCDEF",
        note="已给出 adjust_type",
        question_csv=question_csv,
        now="2026-11-16T10:05:00+08:00",
    )
    assert row["reply_status"] == "ANSWERED"
    stored = list(csv.DictReader(question_csv.open(encoding="utf-8")))
    assert stored[0]["question_id"] == "Q-A1"

    with pytest.raises(ValueError):
        imp.append_question_status(question_id="Q-A1", status="DONE", evidence_ref="x", question_csv=question_csv)
    with pytest.raises(ValueError):
        imp.append_question_status(question_id="A1", status="OPEN", evidence_ref="x", question_csv=question_csv)


def test_invalid_batch_and_channel_rejected(tmp_path: Path) -> None:
    source, staging, register = _setup(tmp_path)
    with pytest.raises(ValueError):
        imp.import_reply(
            source=source,
            batch_id="../escape",
            supplier="iFinD",
            source_channel="email",
            received_at="2026-11-16T10:00:00+08:00",
            reviewed_by="tester",
            staging_root=staging,
            register_csv=register,
        )
    with pytest.raises(ValueError):
        imp.import_reply(
            source=source,
            batch_id="m66_test",
            supplier="iFinD",
            source_channel="carrier-pigeon",
            received_at="2026-11-16T10:00:00+08:00",
            reviewed_by="tester",
            staging_root=staging,
            register_csv=register,
        )
