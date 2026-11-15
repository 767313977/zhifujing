"""M6.6 Batch 1：iFinD 供应商答复本地导入与登记（人工触发，不联网）。

规则：
- 原始答复只追加，不覆盖；同一文件重复导入按 SHA256 识别为 DUPLICATE；
- 未脱敏且命中敏感模式的文件拒绝入库；
- `received_at` 只表示我方收到时间，**不写入 available_at**，也不得当作公告时间；
- 逐题状态只允许 OPEN / ANSWERED / PARTIAL / NOT_SUPPORTED / NEEDS_FOLLOWUP / VERBAL_UNCONFIRMED。

用法：

    python scripts/m66_ifind_reply_import.py --batch m66_20261116 --source reply.eml \
        --supplier iFinD --channel email --received-at 2026-11-16T10:00:00+08:00 \
        --reviewed-by zhangsan --ticket-id IF20261116001 --redaction-status DONE

    python scripts/m66_ifind_reply_import.py --batch m66_20261116 --question Q-A1 \
        --question-status ANSWERED --evidence reply:abc123 --note "已给出 adjust_type 字段"
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGING_ROOT = ROOT / "backend/data/m66_staging"
REGISTER_DIR = ROOT / "reports/trend_engine/m66"
REGISTER_CSV = REGISTER_DIR / "provider_reply_register.csv"
QUESTION_CSV = REGISTER_DIR / "question_reply_status.csv"

REGISTER_FIELDS = [
    "reply_id",
    "supplier",
    "ticket_id",
    "received_at",
    "source_channel",
    "original_filename",
    "stored_path",
    "raw_sha256",
    "redacted_sha256",
    "redaction_status",
    "reviewed_by",
    "review_status",
]
QUESTION_FIELDS = ["question_id", "reply_status", "evidence_ref", "updated_at", "note"]

QUESTION_STATUSES = {
    "OPEN",
    "ANSWERED",
    "PARTIAL",
    "NOT_SUPPORTED",
    "NEEDS_FOLLOWUP",
    "VERBAL_UNCONFIRMED",
}
CHANNELS = {"email", "portal", "wechat", "document"}
REDACTION_STATUSES = {"PENDING", "DONE"}

SENSITIVE_PATTERNS = (
    re.compile(r"bearer\s+[a-z0-9\-._~+/]+=*", re.IGNORECASE),
    re.compile(r"authorization\s*[:=]", re.IGNORECASE),
    re.compile(r"(api[_-]?key|access[_-]?token|refresh[_-]?token|password)\s*[:=]", re.IGNORECASE),
    re.compile(r"\bsk-[a-z0-9]{16,}", re.IGNORECASE),
    re.compile(r"\bcookie\s*[:=]", re.IGNORECASE),
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def scan_sensitive(text: str) -> list[str]:
    return [pattern.pattern for pattern in SENSITIVE_PATTERNS if pattern.search(text)]


def read_register(path: Path = REGISTER_CSV) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _append_row(path: Path, fields: list[str], row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def import_reply(
    *,
    source: Path,
    batch_id: str,
    supplier: str,
    source_channel: str,
    received_at: str,
    reviewed_by: str,
    ticket_id: str | None = None,
    redaction_status: str = "PENDING",
    staging_root: Path = STAGING_ROOT,
    register_csv: Path = REGISTER_CSV,
) -> dict:
    if source_channel not in CHANNELS:
        raise ValueError(f"未知渠道：{source_channel}")
    if redaction_status not in REDACTION_STATUSES:
        raise ValueError(f"未知脱敏状态：{redaction_status}")
    if not re.fullmatch(r"[0-9A-Za-z_\-]+", batch_id):
        raise ValueError(f"批次号非法：{batch_id}")
    source = Path(source)
    if not source.exists():
        raise FileNotFoundError(source)

    raw = source.read_bytes()
    digest = sha256_bytes(raw)
    existing = read_register(register_csv)
    if any(row.get("raw_sha256") == digest for row in existing):
        return {"status": "DUPLICATE", "raw_sha256": digest, "reply_id": None}

    text = raw.decode("utf-8", errors="replace")
    findings = scan_sensitive(text)
    if findings and redaction_status != "DONE":
        raise ValueError(f"文件命中敏感模式且未标记已脱敏：{findings}")

    target_dir = staging_root / batch_id / "ifind_reply"
    target_dir.mkdir(parents=True, exist_ok=True)
    stored = target_dir / f"{digest[:16]}_{source.name}"
    if stored.exists():
        raise FileExistsError(f"证据文件已存在，拒绝覆盖：{stored}")
    shutil.copyfile(source, stored)

    try:
        stored_path = str(stored.relative_to(ROOT))
    except ValueError:
        stored_path = str(stored)

    row = {
        "reply_id": digest[:16],
        "supplier": supplier,
        "ticket_id": ticket_id or "",
        "received_at": received_at,
        "source_channel": source_channel,
        "original_filename": source.name,
        "stored_path": stored_path,
        "raw_sha256": digest,
        "redacted_sha256": digest if redaction_status == "DONE" else "",
        "redaction_status": redaction_status,
        "reviewed_by": reviewed_by,
        "review_status": "OPEN",
    }
    _append_row(register_csv, REGISTER_FIELDS, row)
    return {"status": "IMPORTED", "raw_sha256": digest, "reply_id": row["reply_id"], "stored_path": row["stored_path"]}


def append_question_status(
    *,
    question_id: str,
    status: str,
    evidence_ref: str,
    note: str = "",
    question_csv: Path = QUESTION_CSV,
    now: str | None = None,
) -> dict:
    if status not in QUESTION_STATUSES:
        raise ValueError(f"未知题目状态：{status}")
    if not re.fullmatch(r"Q-[A-D]\d{1,2}", question_id):
        raise ValueError(f"问题编号非法：{question_id}")
    row = {
        "question_id": question_id,
        "reply_status": status,
        "evidence_ref": evidence_ref,
        "updated_at": now or datetime.now(timezone.utc).isoformat(),
        "note": note,
    }
    _append_row(question_csv, QUESTION_FIELDS, row)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description="iFinD 答复导入/题目状态登记（本地，不联网）")
    parser.add_argument("--batch", required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--supplier", default="iFinD")
    parser.add_argument("--channel", default="email")
    parser.add_argument("--received-at")
    parser.add_argument("--reviewed-by", default="")
    parser.add_argument("--ticket-id")
    parser.add_argument("--redaction-status", default="PENDING")
    parser.add_argument("--question")
    parser.add_argument("--question-status")
    parser.add_argument("--evidence", default="")
    parser.add_argument("--note", default="")
    args = parser.parse_args()

    if args.question and args.question_status:
        print(append_question_status(
            question_id=args.question,
            status=args.question_status,
            evidence_ref=args.evidence,
            note=args.note,
        ))
        return 0

    if not args.source or not args.received_at:
        parser.error("导入答复需要 --source 与 --received-at（或使用 --question/--question-status）")
    result = import_reply(
        source=args.source,
        batch_id=args.batch,
        supplier=args.supplier,
        source_channel=args.channel,
        received_at=args.received_at,
        reviewed_by=args.reviewed_by,
        ticket_id=args.ticket_id,
        redaction_status=args.redaction_status,
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
