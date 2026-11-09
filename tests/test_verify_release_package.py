"""P1.2-F5 发布包核验脚本的失败用例。

每个用例都必须在 `verify()` 里给出明确错误码；CLI 另有两条退出码用例。
用临时 Git 仓库构造「冻结源码」，避免依赖真实仓库状态。
"""

import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_release_package.py"
sys.path.insert(0, str(ROOT / "scripts"))

import verify_release_package as vrp  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _make_repo(tmp_path: Path, files: dict[str, bytes]) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    for path, data in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    commit = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    return repo, commit, tree


def _member(name: str, data: bytes = b"", mode: int = 0o644, mtime: int = 0, type_=tarfile.REGTYPE,
            linkname: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = mtime
    info.type = type_
    info.linkname = linkname
    return info


def _write_tar(path: Path, items: list[tuple[tarfile.TarInfo, bytes]]) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for info, data in items:
            tar.addfile(info, io.BytesIO(data) if info.isfile() else None)


def _manifest(path: Path, tree: str, dist: list[tuple[str, bytes]]) -> Path:
    files = [
        {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in dist
    ]
    digest = hashlib.sha256(
        "\n".join(f"{item['path']}:{item['sha256']}" for item in files).encode()
    ).hexdigest()
    payload = {
        "git": {"tree": tree},
        "frontend_dist": {"files": files, "digest": digest},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture()
def base(tmp_path):
    repo, commit, tree = _make_repo(tmp_path, {"backend/app/x.py": b"print(1)\n"})
    manifest = _manifest(tmp_path / "manifest.json", tree, [])
    return repo, commit, manifest, tmp_path


def _verify(base, items, frozen=None, manifest=None):
    repo, commit, base_manifest, tmp_path = base
    archive = tmp_path / "pkg.tar.gz"
    _write_tar(archive, items)
    return vrp.verify(
        archive, manifest or base_manifest, frozen or commit, repo=repo
    )


def _codes(result):
    return {item["code"] for item in result["errors"]}


def test_valid_package_passes(base):
    result = _verify(base, [(_member("fupan/backend/app/x.py", b"print(1)\n"), b"print(1)\n")])
    assert result["ok"] is True, result["errors"]


def test_forbidden_file_is_reported(base):
    result = _verify(
        base,
        [
            (_member("fupan/backend/app/x.py", b"print(1)\n"), b"print(1)\n"),
            (_member("fupan/.env", b"TOKEN=1"), b"TOKEN=1"),
        ],
    )
    assert not result["ok"]
    assert "forbidden_entry" in _codes(result)


def test_modified_app_bytes_are_reported(base):
    result = _verify(base, [(_member("fupan/backend/app/x.py", b"print(2)\n"), b"print(2)\n")])
    assert not result["ok"]
    assert {"byte_mismatch", "blob_mismatch"} & _codes(result)


def test_modified_dist_artifact_is_reported(base):
    repo, commit, manifest, tmp_path = base
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    dist_manifest = _manifest(tmp_path / "m2.json", tree, [("a.js", b"good")])
    result = _verify(
        base,
        [(_member("fupan/backend/app/x.py", b"print(1)\n"), b"print(1)\n"),
         (_member("fupan/frontend/dist/a.js", b"bad"), b"bad")],
        manifest=dist_manifest,
    )
    assert not result["ok"]
    assert "dist_mismatch" in _codes(result)


def test_duplicate_path_is_reported(base):
    result = _verify(
        base,
        [
            (_member("fupan/backend/app/x.py", b"print(1)\n"), b"print(1)\n"),
            (_member("fupan/backend/app/x.py", b"print(1)\n"), b"print(1)\n"),
        ],
    )
    assert not result["ok"]
    assert "duplicate_path" in _codes(result)


def test_path_traversal_is_reported(base):
    result = _verify(base, [(_member("fupan/../evil.py", b"x"), b"x")])
    assert not result["ok"]
    assert "path_traversal" in _codes(result)


def test_bad_mode_is_reported(base):
    result = _verify(
        base, [(_member("fupan/backend/app/x.py", b"print(1)\n", mode=0o755), b"print(1)\n")]
    )
    assert not result["ok"]
    assert "bad_mode" in _codes(result)


def test_wrong_commit_is_reported(base):
    repo, first, manifest, tmp_path = base
    (repo / "backend/app/x.py").write_bytes(b"print(2)\n")
    _git(repo, "commit", "-q", "-am", "second")
    result = _verify(
        base,
        [(_member("fupan/backend/app/x.py", b"print(2)\n"), b"print(2)\n")],
        frozen=first,
        manifest=manifest,
    )
    assert not result["ok"]
    assert "blob_mismatch" in _codes(result)


def test_manifest_digest_change_is_reported(base):
    repo, commit, manifest_path, tmp_path = base
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["frontend_dist"]["digest"] = "0" * 64
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    result = _verify(
        base,
        [(_member("fupan/backend/app/x.py", b"print(1)\n"), b"print(1)\n")],
        manifest=manifest_path,
    )
    assert not result["ok"]
    assert "dist_digest_mismatch" in _codes(result)


def test_symlink_and_special_files_are_reported(base):
    result = _verify(
        base,
        [
            (_member("fupan/backend/app/x.py", b"print(1)\n"), b"print(1)\n"),
            (_member("fupan/link", type_=tarfile.SYMTYPE, linkname="/etc/passwd"), b""),
            (_member("fupan/fifo", type_=tarfile.FIFOTYPE), b""),
        ],
    )
    assert not result["ok"]
    assert {"symlink", "special_file"} <= _codes(result)


def test_case_collision_and_mtime_are_reported(base):
    result = _verify(
        base,
        [
            (_member("fupan/backend/app/x.py", b"print(1)\n"), b"print(1)\n"),
            (_member("fupan/backend/app/X.py", b"print(1)\n"), b"print(1)\n"),
            (_member("fupan/backend/app/x.py", b"print(1)\n", mtime=123), b"print(1)\n"),
        ],
    )
    assert not result["ok"]
    assert {"case_collision", "nondeterministic_mtime", "duplicate_path"} & _codes(result)


def test_crlf_shell_script_is_reported(base):
    result = _verify(
        base,
        [
            (_member("fupan/backend/app/x.py", b"print(1)\n"), b"print(1)\n"),
            (_member("fupan/deploy/x.sh", b"#!/bin/sh\r\n"), b"#!/bin/sh\r\n"),
        ],
    )
    assert not result["ok"]
    assert "crlf_in_shell" in _codes(result)


def test_cli_exit_codes(base, tmp_path):
    repo, commit, manifest, _ = base
    good = tmp_path / "good.tar.gz"
    _write_tar(good, [(_member("fupan/backend/app/x.py", b"print(1)\n"), b"print(1)\n")])
    bad = tmp_path / "bad.tar.gz"
    _write_tar(bad, [(_member("fupan/.env", b"x"), b"x")])

    def run(archive: Path):
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--archive",
                str(archive),
                "--manifest",
                str(manifest),
                "--frozen",
                commit,
                "--repo",
                str(repo),
            ],
            cwd=str(repo),
            capture_output=True,
            text=True,
        ).returncode

    assert run(good) == 0
    assert run(bad) == 1
