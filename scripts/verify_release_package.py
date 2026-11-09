"""核验发布包（P1.2-F2 / F3 / F5）。

输入：`scripts/pack_deploy.py` 生成的 tar.gz、RC 清单（`rc-manifest.json`）、
冻结 commit。输出：工件 SHA256、逐文件摘要、以及下列检查结果：

1. **路径安全**：POSIX 相对路径、无绝对路径、无 `..`、无反斜杠、无重复成员、
   大小写折叠后无冲突；
2. **成员类型**：只允许普通文件与目录；拒绝符号链接、硬链接、设备、FIFO；
3. **权限**：文件 0644、目录 0755；不接受可执行位或组/其他可写；
4. **确定性**：成员 mtime 必须为 0（打包时统一），Shell 脚本必须是 LF；
5. **内容**：
   - 实际字节 SHA256 与工作区（= HEAD）一致；
   - Git 规范化 blob 与冻结 commit 一致（不能只比其中一种）；
   - 前端产物与 RC 清单逐文件摘要一致、汇总摘要一致；
6. **集合**：tar 内文件集合 = 冻结 commit 的白名单文件 + 清单里的构建产物，
   不多不少；文件数与清单闭合。

用法：
    python scripts/verify_release_package.py \
        --archive dist-deploy/fupan-fe2005f.tar.gz \
        --manifest docs/p1.1/rc-manifest.json \
        --frozen fe2005f226015cb97a175c2aadc47a87f8085f50 \
        --out docs/p1.2/release-package.json

返回码：0 = 全部通过；1 = 有失败项（逐条打印 code 与原因）。
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
SOURCE_TREE_PATHS = ("backend/app", "backend/requirements.txt", "scripts", "deploy")
#: 只用于开发机、进包没有意义的脚本（P1.2-F4：生产包最小权限）。
DEV_ONLY = (
    "scripts/git-push.ps1",
    "scripts/pack_deploy.py",
    "scripts/release_manifest.py",
    "scripts/verify_release_package.py",
)
ARCHIVE_ROOT = "fupan"


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(repo: Path, *args: str) -> str | None:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace").strip()


def _normalize(name: str) -> tuple[str | None, str | None]:
    """把 tar 成员名规范化为相对路径；返回 (路径, 错误码)。"""
    if "\\" in name:
        return None, "path_backslash"
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return None, "path_absolute"
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        return None, "path_traversal"
    if parts and parts[0] == ARCHIVE_ROOT:
        parts = parts[1:]
    if not parts:
        return None, "path_empty"
    return "/".join(parts), None


def inspect_members(archive: Path) -> tuple[dict, list[dict]]:
    """逐成员检查路径、类型、权限与确定性；返回 (文件表, 问题列表)。"""
    files: dict[str, dict] = {}
    issues: list[dict] = []
    seen: set[str] = set()
    folded: dict[str, str] = {}

    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            path, error = _normalize(member.name)
            if error:
                issues.append({"code": error, "detail": member.name})
                continue
            if member.isdir():
                if member.mode != 0o755:
                    issues.append({"code": "bad_mode", "detail": f"{path} dir={oct(member.mode)}"})
                continue
            if member.issym():
                issues.append({"code": "symlink", "detail": path})
                continue
            if member.islnk():
                issues.append({"code": "hardlink", "detail": path})
                continue
            if not member.isfile():
                issues.append({"code": "special_file", "detail": f"{path} type={member.type}"})
                continue
            if path in seen:
                issues.append({"code": "duplicate_path", "detail": path})
                continue
            key = path.casefold()
            if key in folded:
                issues.append(
                    {"code": "case_collision", "detail": f"{folded[key]} ↔ {path}"}
                )
            seen.add(path)
            folded[key] = path
            if member.mode != 0o644:
                issues.append({"code": "bad_mode", "detail": f"{path} mode={oct(member.mode)}"})
            if member.mtime != 0:
                issues.append({"code": "nondeterministic_mtime", "detail": f"{path} mtime={member.mtime}"})
            data = tar.extractfile(member).read()
            if path.endswith(".sh") and b"\r\n" in data:
                issues.append({"code": "crlf_in_shell", "detail": path})
            files[path] = {"size": len(data), "sha256": sha_bytes(data), "data": data}
    return files, issues


def expected_sources(repo: Path, frozen: str) -> list[str]:
    listed = _git(
        repo,
        "ls-tree",
        "-r",
        "--name-only",
        frozen,
        "--",
        *SOURCE_TREE_PATHS,
    )
    if listed is None:
        return []
    return [path for path in listed.splitlines() if path and path not in DEV_ONLY]


def verify(archive: Path, manifest_path: Path, frozen_ref: str, repo: Path = ROOT) -> dict:
    errors: list[dict] = []
    full = _git(repo, "rev-parse", frozen_ref)
    if not full:
        return {
            "ok": False,
            "errors": [{"code": "frozen_not_found", "detail": frozen_ref}],
            "archive": str(archive),
        }

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files, issues = inspect_members(archive)
    errors.extend(issues)
    contents = {path: item.pop("data") for path, item in files.items()}

    dist_expected = {item["path"]: item["sha256"] for item in manifest["frontend_dist"]["files"]}
    dist_actual = {
        path[len("frontend/dist/") :]: item["sha256"]
        for path, item in files.items()
        if path.startswith("frontend/dist/")
    }

    for path in files:
        if any(path == token or path.startswith(token) for token in FORBIDDEN_PREFIX) or path.endswith(
            FORBIDDEN_SUFFIX
        ):
            errors.append({"code": "forbidden_entry", "detail": path})

    # 集合：冻结 commit 白名单源码 + 清单构建产物，必须不多不少
    expected = set(expected_sources(repo, full)) | {f"frontend/dist/{p}" for p in dist_expected}
    actual = set(files)
    for path in sorted(actual - expected):
        errors.append({"code": "unexpected_file", "detail": path})
    for path in sorted(expected - actual):
        errors.append({"code": "missing_file", "detail": path})

    # 内容：实际字节 vs 工作区；Git blob vs 冻结 commit
    byte_mismatch: list[str] = []
    blob_mismatch: list[dict] = []
    source_checked = 0
    with tempfile.TemporaryDirectory() as tmp:
        temp = Path(tmp) / "blob"
        for path in sorted(expected):
            if path.startswith("frontend/dist/") or path not in contents:
                continue
            source_checked += 1
            working = repo / path
            if not working.exists() or working.read_bytes() != contents[path]:
                byte_mismatch.append(path)
            expected_blob = _git(repo, "rev-parse", f"{full}:{path}")
            temp.write_bytes(contents[path])
            actual_blob = _git(repo, "hash-object", f"--path={path}", str(temp))
            if expected_blob is None or actual_blob != expected_blob:
                blob_mismatch.append(
                    {"path": path, "blob_expected": expected_blob, "blob_actual": actual_blob}
                )
    for path in byte_mismatch:
        errors.append({"code": "byte_mismatch", "detail": path})
    errors.extend({"code": "blob_mismatch", **item} for item in blob_mismatch)

    for path in sorted(set(dist_actual) & set(dist_expected)):
        if dist_actual[path] != dist_expected[path]:
            errors.append({"code": "dist_mismatch", "detail": path})
    for path in sorted(set(dist_expected) - set(dist_actual)):
        errors.append({"code": "dist_missing", "detail": path})
    for path in sorted(set(dist_actual) - set(dist_expected)):
        errors.append({"code": "dist_extra", "detail": path})

    dist_digest = hashlib.sha256(
        "\n".join(
            f"{item['path']}:{dist_actual.get(item['path'])}"
            for item in manifest["frontend_dist"]["files"]
        ).encode()
    ).hexdigest()
    if dist_digest != manifest["frontend_dist"]["digest"]:
        errors.append(
            {
                "code": "dist_digest_mismatch",
                "detail": f"{manifest['frontend_dist']['digest']} != {dist_digest}",
            }
        )

    tree = _git(repo, "rev-parse", f"{full}^{{tree}}")
    if manifest.get("git", {}).get("tree") and tree != manifest["git"]["tree"]:
        errors.append(
            {"code": "tree_mismatch", "detail": f"{manifest['git']['tree']} != {tree}"}
        )

    return {
        "ok": not errors,
        "errors": errors,
        "archive": str(archive),
        "archive_size": archive.stat().st_size,
        "archive_sha256": sha_file(archive),
        "frozen_commit": full,
        "frozen_tree": tree,
        "file_count": len(files),
        "expected_count": len(expected),
        "top_level": sorted({path.split("/", 1)[0] for path in files}),
        "source_files_checked": source_checked,
        "byte_mismatch": byte_mismatch,
        "blob_mismatch": blob_mismatch,
        "dist_file_count": len(dist_actual),
        "dist_expected_digest": manifest["frontend_dist"]["digest"],
        "dist_actual_digest": dist_digest,
        "files": {
            path: {"size": item["size"], "sha256": item["sha256"]}
            for path, item in sorted(files.items(), key=lambda pair: pair[0].casefold())
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="核验发布包与冻结源码/构建产物是否一致")
    parser.add_argument("--archive", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--frozen", required=True, help="冻结 commit（完整或短 SHA）")
    parser.add_argument(
        "--repo", default=str(ROOT), help="Git 仓库根（默认当前项目；自测时指向临时仓库）"
    )
    parser.add_argument("--out")
    args = parser.parse_args()

    archive = Path(args.archive)
    if not archive.exists():
        raise SystemExit(f"发布包不存在：{archive}")
    payload = verify(archive, Path(args.manifest), args.frozen, repo=Path(args.repo))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    summary = {
        key: payload.get(key)
        for key in (
            "ok",
            "frozen_commit",
            "frozen_tree",
            "archive_sha256",
            "archive_size",
            "file_count",
            "expected_count",
            "top_level",
            "source_files_checked",
            "dist_file_count",
            "dist_expected_digest",
            "dist_actual_digest",
        )
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not payload["ok"]:
        print(f"失败项 {len(payload['errors'])}：")
        for item in payload["errors"]:
            print(f"  [{item['code']}] {item.get('detail', '')}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
