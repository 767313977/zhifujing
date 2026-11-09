"""核验发布包（P1.2-B）。

输入：由 `scripts/pack_deploy.py` 生成的 tar.gz、RC 清单（`rc-manifest.json`）、
冻结 commit。输出：工件 SHA256、逐文件摘要、禁止项扫描、源码/构建产物一致性。

校验口径（两个容易踩的坑）：

1. **不能直接比 `sha256(git show)`**：工作区是 CRLF、Git blob 是 LF，
   逐字节比较会全部不一致。这里用 `git hash-object --path=<路径>` 让 Git
   应用同一套换行过滤，再与 `git rev-parse <commit>:<路径>` 比较 blob SHA。
2. **构建产物按清单记录的顺序复算摘要**：`frontend_dist.digest` 生成时沿用了
   Windows `PurePath` 的不区分大小写排序，Linux 上 `sorted()` 顺序可能不同。

用法：
    python scripts/verify_release_package.py \
        --archive dist-deploy/fupan-fe2005f.tar.gz \
        --manifest docs/p1.1/rc-manifest.json \
        --frozen fe2005f226015cb97a175c2aadc47a87f8085f50 \
        --out docs/p1.2/release-package.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_PREFIX = (
    ".env",
    ".git/",
    "zhifujing/",
    "backend/data/",
    ".screenshots/",
    "tests/",
    "docs/",
    "dist-deploy/",
)
FORBIDDEN_SUFFIX = (".db", ".db-wal", ".db-shm", ".log", ".pyc", ".pem", ".key")
SOURCE_PREFIX = ("backend/", "scripts/", "deploy/")


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(*args: str) -> str | None:
    result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True)
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace").strip()


def verify(archive: Path, manifest_path: Path, frozen: str) -> dict:
    files: dict[str, dict] = {}
    contents: dict[str, bytes] = {}
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            relative = member.name.split("/", 1)[1] if "/" in member.name else member.name
            data = tar.extractfile(member).read()
            files[relative] = {"size": len(data), "sha256": sha_bytes(data)}
            contents[relative] = data

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dist_expected = {item["path"]: item["sha256"] for item in manifest["frontend_dist"]["files"]}

    forbidden = [
        path
        for path in files
        if any(path == token or path.startswith(token) for token in FORBIDDEN_PREFIX)
        or path.endswith(FORBIDDEN_SUFFIX)
    ]

    source_mismatch = []
    source_checked = 0
    with tempfile.TemporaryDirectory() as tmp:
        temp = Path(tmp) / "blob"
        for path, data in contents.items():
            if path.startswith("frontend/dist/") or not path.startswith(SOURCE_PREFIX):
                continue
            expected = git("rev-parse", f"{frozen}:{path}")
            temp.write_bytes(data)
            actual = git("hash-object", f"--path={path}", str(temp))
            source_checked += 1
            if expected is None or actual != expected:
                source_mismatch.append(
                    {"path": path, "blob_expected": expected, "blob_actual": actual}
                )

    dist_actual = {
        path[len("frontend/dist/") :]: item["sha256"]
        for path, item in files.items()
        if path.startswith("frontend/dist/")
    }
    dist_digest = hashlib.sha256(
        "\n".join(
            f"{item['path']}:{dist_actual.get(item['path'])}"
            for item in manifest["frontend_dist"]["files"]
        ).encode()
    ).hexdigest()

    return {
        "archive": str(archive),
        "archive_size": archive.stat().st_size,
        "archive_sha256": sha_file(archive),
        "file_count": len(files),
        "top_level": sorted({path.split("/", 1)[0] for path in files}),
        "forbidden_entries": forbidden,
        "source_files_checked": source_checked,
        "source_mismatch": source_mismatch,
        "dist_expected_digest": manifest["frontend_dist"]["digest"],
        "dist_actual_digest": dist_digest,
        "dist_file_count": len(dist_actual),
        "dist_mismatch": [
            path for path, digest in dist_actual.items() if dist_expected.get(path) != digest
        ],
        "dist_missing": sorted(set(dist_expected) - set(dist_actual)),
        "dist_extra": sorted(set(dist_actual) - set(dist_expected)),
        "frozen_commit": frozen,
        "files": dict(sorted(files.items(), key=lambda item: item[0].casefold())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="核验发布包与冻结源码/构建产物是否一致")
    parser.add_argument("--archive", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--frozen", required=True, help="冻结 commit（完整或短 SHA）")
    parser.add_argument("--out")
    args = parser.parse_args()

    archive = Path(args.archive)
    if not archive.exists():
        raise SystemExit(f"发布包不存在：{archive}")
    full = git("rev-parse", args.frozen)
    if not full:
        raise SystemExit(f"commit 不存在：{args.frozen}")

    payload = verify(archive, Path(args.manifest), full)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    summary = {
        key: payload[key]
        for key in (
            "archive_sha256",
            "archive_size",
            "file_count",
            "top_level",
            "forbidden_entries",
            "source_files_checked",
            "source_mismatch",
            "dist_expected_digest",
            "dist_actual_digest",
            "dist_file_count",
            "dist_mismatch",
            "dist_missing",
            "dist_extra",
        )
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    ok = (
        not payload["forbidden_entries"]
        and not payload["source_mismatch"]
        and not payload["dist_mismatch"]
        and not payload["dist_missing"]
        and not payload["dist_extra"]
        and payload["dist_expected_digest"] == payload["dist_actual_digest"]
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
