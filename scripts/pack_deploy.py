"""把项目打成一个能直接传到服务器解压的包。

为什么不用 `tar czf .` 打包整个目录：

- `.venv`（几百 MB）和 `node_modules` 都不该传，服务器上重新装
- **SQLite 不能直接拷文件**：库跑在 WAL 模式下，`fupan.db` 与 `fupan.db-wal`
  必须成对拷贝才一致。这里走 sqlite3 的 backup API 做在线快照，
  所以**不用停掉本机正在跑的后端**

白名单而不是黑名单：只有 `INCLUDE` 里列出的路径会进包，
所以 `.venv` / `node_modules` 这类大块头不需要额外的排除规则，也不会漏写。

产出 `dist-deploy/fupan-<日期>.tar.gz`。传到服务器后：

    tar xzf fupan-<日期>.tar.gz -C /opt      # 解开是 /opt/fupan
    cd /opt/fupan && sudo bash deploy/install.sh

用法::

    python scripts/pack_deploy.py                # 完整包（含数据库，首次部署用）
    python scripts/pack_deploy.py --no-db        # 只打代码（日常更新用）
    python scripts/pack_deploy.py --out D:\\fupan.tar.gz

为什么日常更新要用 `--no-db`：

- 数据库是**数据**不是代码，云端每天自己采集生成，更新代码不该碰它；
- 完整包解压时会用**本机数据库覆盖服务器数据库**，把服务器最新采集的数据
  退回成本机旧数据 —— 更新代码时这是错的；
- 数据库 116 MB 占了包的绝大部分，省掉它之后包只有几百 KB，上传秒完成。
"""

import argparse
import sqlite3
import sys
import tarfile
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "backend" / "data" / "fupan.db"
DEFAULT_OUT_DIR = ROOT / "dist-deploy"
ARCHIVE_ROOT = "fupan"

# 要打进包里的路径（相对项目根）。`.env` 也在里面 —— 它是密钥，
# 传的时候走 scp/ssh 这种加密通道，别放公开网盘。
INCLUDE = (
    "backend/app",
    "backend/requirements.txt",
    "frontend/dist",
    "scripts",
    "deploy",
    "docs",
    ".env",
)

SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SKIP_SUFFIX = (".pyc", ".pyo")


def snapshot_db(target: Path) -> None:
    """在线备一份一致的 SQLite 副本。

    直接拷 `.db` 文件会漏掉还留在 `-wal` 里的最近写入 —— 拷出来的库可能
    「缺最后几笔」甚至半损坏，而且这种损坏在服务器上要等到某次查询才暴露。
    走 backup API 才是安全的，代价是这一步必须在本机后端运行时也能做
    （它自己会处理并发写）。
    """
    if not DB.exists():
        raise SystemExit(f"没找到数据库 {DB}，先在本机跑一次采集再来打包")
    target.parent.mkdir(parents=True, exist_ok=True)
    # URI 形式要用正斜杠，Windows 的反斜杠会被当成转义
    source = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def add_tree(tar: tarfile.TarFile, src: Path, prefix: str) -> int:
    """把一个目录塞进包，顺带跳过缓存目录与字节码。返回文件数。"""
    count = 0
    for path in sorted(src.rglob("*")):
        relative = path.relative_to(src)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.suffix in SKIP_SUFFIX:
            continue
        # recursive=False：目录只留一个条目，内容由各自的循环负责，
        # 否则 tarfile 会把整棵树重复加一遍
        tar.add(path, arcname=f"{prefix}/{relative.as_posix()}", recursive=False)
        if path.is_file():
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="打包部署产物")
    parser.add_argument(
        "--out",
        type=Path,
        help=f"输出文件，缺省 {DEFAULT_OUT_DIR}/fupan-<日期>.tar.gz",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="只打代码、不含数据库。代码更新时用它：不会用本机库覆盖服务器最新数据",
    )
    args = parser.parse_args()

    out = args.out or DEFAULT_OUT_DIR / f"fupan-{date.today().isoformat()}.tar.gz"
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    files = 0
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(out, "w:gz") as tar:
        for item in INCLUDE:
            src = ROOT / item
            if not src.exists():
                # frontend/dist 没构建过就跳过 —— 云端只做推送时本来也不需要它
                print(f"  跳过（不存在）: {item}")
                continue
            if src.is_dir():
                files += add_tree(tar, src, f"{ARCHIVE_ROOT}/{item}")
            else:
                tar.add(src, arcname=f"{ARCHIVE_ROOT}/{item}")
                files += 1

        if not args.no_db:
            snapshot = Path(tmp) / "fupan.db"
            snapshot_db(snapshot)
            tar.add(snapshot, arcname=f"{ARCHIVE_ROOT}/backend/data/fupan.db")
            files += 1

    size = out.stat().st_size / 1024 / 1024
    print(f"已打包 {files} 个文件，{size:.1f} MB -> {out}")
    if args.no_db:
        print("（代码包，不含数据库 —— 解压不会覆盖服务器已有的数据）")
    print(f"传到服务器：scp \"{out}\" <用户>@<服务器IP>:~")
    print(f"然后在服务器上：tar xzf {out.name} -C /opt && cd /opt/fupan && sudo bash deploy/install.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
