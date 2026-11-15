"""生成 frontend/dist 的确定性 manifest（S6.8 OLD/NEW 版本证据）。

输出：
- files: 每个文件的相对路径 + size + sha256
- manifest_sha256: 对「path sha256」按路径排序后的文本做 SHA256

用法：
    python scripts/s6_8_dist_manifest.py --dist <dir> --out <json>
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    dist = Path(args.dist).resolve()
    files = []
    for path in sorted(dist.rglob("*")):
        if not path.is_file():
            continue
        files.append(
            {
                "path": path.relative_to(dist).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256(path),
            }
        )
    lines = "\n".join(f"{item['path']} {item['sha256']}" for item in files)
    manifest = hashlib.sha256(lines.encode("utf-8")).hexdigest()
    payload = {
        "dist": str(dist),
        "file_count": len(files),
        "manifest_sha256": manifest,
        "files": files,
    }
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"file_count": len(files), "manifest_sha256": manifest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
