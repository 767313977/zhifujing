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

**不收「未跟踪且未被忽略」的文件**（2026-10-10 加）：

`add_tree` 是按目录收文件的，所以**未跟踪的新文件也会进包**。踩过一次：另一端正在写
`backend/app/api/ai.py`（未跟踪），部署会把它连同 `main.py` 里那两行
`include_router(ai.router)` 一起发上线 —— 等于把别人没写完的功能发到生产。

判据用「**未跟踪 且 未被忽略**」，而**不是**「只收 git 跟踪的文件」：

- `frontend/dist/`（构建产物）与 `.env`（密钥）都被 gitignore、都**未跟踪**，但它们
  **必须**进包 —— 用「只收跟踪文件」会把这两个丢掉（前端没产物、服务器没 token）；
- 「未跟踪且未被忽略」刚好把这两种情况分开：构建产物/密钥**被忽略**、别人在写的源码**不被忽略**。

跳过的文件会在输出里逐条列出来，不静默；要恢复旧行为用 `--allow-untracked`。

**「已跟踪但有未提交改动」的直接拦下**（要放行得加 `--allow-dirty`）：那种情况下包里带的是
**工作区版本**，会让包处于「一半提交」的状态。光排除未跟踪文件还不够 —— 例如 `main.py`
里那行 `include_router(ai.router)` 在工作区里、而 `api/ai.py` 还没 `git add`：未跟踪的那个
被排除了，`main.py` 却照样进包，于是**包里的服务一起来就 import 失败**。这两条必须一起看，
只做前者会给人虚假的安全感。
"""

import argparse
import sqlite3
import subprocess
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


def _git(*args: str) -> list[str] | None:
    """跑一条只读 git 命令、按行返回。git 不可用（比如不在仓库里跑）时返回 None。"""
    try:
        result = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return [line for line in result.stdout.splitlines() if line.strip()]


def untracked_files() -> set[str]:
    """「未跟踪且未被忽略」的文件（相对项目根）—— 别人正在写、还没进仓库的东西。

    判据为什么是这一条而不是「只收 git 跟踪的文件」，见模块开头那段：构建产物
    （`frontend/dist`）与密钥（`.env`）都是**未跟踪**但**必须**进包的，区别在于它们
    **被忽略**。git 用不了时返回空集（退回旧行为），只提示一句，不拦。
    """
    listed = _git("ls-files", "--others", "--exclude-standard")
    if listed is None:
        print("  ⚠️ 读不到 git 未跟踪清单（git 不可用？），本次不过滤未跟踪文件")
        return set()
    return set(listed)


def _in_include(path: str) -> bool:
    """这个仓库内路径会不会进包（在 `INCLUDE` 的某个条目下面）。"""
    return any(path == item or path.startswith(f"{item}/") for item in INCLUDE)


def dirty_tracked() -> list[str]:
    """要进包、但工作区有未提交改动的**已跟踪**文件。

    这类文件发上去的是**工作区版本**而不是提交版。危险在于它会让包处于「一半提交、
    一半没提交」的状态：比如新文件（`api/ai.py`）还没 `git add`，而引用它的改动
    （`main.py` 里那行 `include_router(ai.router)`）已经在工作区里 —— 未跟踪那个被
    本文件排除了，但 main.py 照样进包，于是**包里的服务一起来就 import 失败**。
    所以这一类比未跟踪文件更该拦（见 `main` 里的处理）。
    """
    listed = _git("status", "--porcelain")
    if listed is None:
        return []
    out: list[str] = []
    for line in listed:
        if line.startswith("??"):
            continue
        # porcelain: `XY<空格>路径`；重命名是 `R  old -> new`
        path = line[3:].strip().split(" -> ")[-1].strip('"')
        if _in_include(path):
            out.append(line)
    return out


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


def add_tree(
    tar: tarfile.TarFile, src: Path, prefix: str, *, skip: set[str] | None = None
) -> tuple[int, list[str]]:
    """把一个目录塞进包，跳过缓存目录、字节码，以及 `skip` 里的文件。

    返回（打进包的文件数, 被跳过的相对路径列表）—— 跳过的要能报出来，别静默。
    """
    skip = skip or set()
    count = 0
    skipped: list[str] = []
    for path in sorted(src.rglob("*")):
        relative = path.relative_to(src)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.suffix in SKIP_SUFFIX:
            continue
        if path.is_file():
            repo_relative = path.relative_to(ROOT).as_posix()
            if repo_relative in skip:
                skipped.append(repo_relative)
                continue
        # recursive=False：目录只留一个条目，内容由各自的循环负责，
        # 否则 tarfile 会把整棵树重复加一遍
        tar.add(path, arcname=f"{prefix}/{relative.as_posix()}", recursive=False)
        if path.is_file():
            count += 1
    return count, skipped


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
    parser.add_argument(
        "--allow-untracked",
        action="store_true",
        help="把「未跟踪且未被忽略」的文件也打进包（默认不收，见模块说明）",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="允许包里有「已跟踪但未提交」的文件（默认拦下，见模块说明）",
    )
    args = parser.parse_args()

    out = args.out or DEFAULT_OUT_DIR / f"fupan-{date.today().isoformat()}.tar.gz"
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    # 不收「未跟踪且未被忽略」的文件（见模块说明）：那是还没进仓库的东西，可能是
    # 别人正在写的代码，不该跟着部署发上线
    skip = set() if args.allow_untracked else untracked_files()

    # 「已跟踪但有未提交改动」的直接拦下（比未跟踪那类更危险）：包里会是工作区版本，
    # 于是包可能处于「新文件没 add、引用它的改动却在里面」的半提交状态 —— 发上去
    # 服务一起来就 import 失败。要强行打包得显式加 --allow-dirty。
    dirty = [] if args.allow_dirty else dirty_tracked()
    if dirty:
        print(f"✗ 有 {len(dirty)} 个要进包的文件「已跟踪但未提交改动」，包里会是工作区版本：")
        for line in dirty:
            print(f"      {line}")
        print("  先 git add/commit 或还原它们；确认无碍要强行打包，加 --allow-dirty 重跑。")
        return 1

    files = 0
    skipped: list[str] = []
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(out, "w:gz") as tar:
        for item in INCLUDE:
            src = ROOT / item
            if not src.exists():
                # frontend/dist 没构建过就跳过 —— 云端只做推送时本来也不需要它
                print(f"  跳过（不存在）: {item}")
                continue
            if src.is_dir():
                count, tree_skipped = add_tree(
                    tar, src, f"{ARCHIVE_ROOT}/{item}", skip=skip
                )
                files += count
                skipped += tree_skipped
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
    if skipped:
        print(f"  ⚠️ 跳过 {len(skipped)} 个未跟踪文件（不属于仓库，可能是别人在写的代码）：")
        for path in skipped:
            print(f"      {path}")
    if args.no_db:
        print("（代码包，不含数据库 —— 解压不会覆盖服务器已有的数据）")
    print(f"传到服务器：scp \"{out}\" <用户>@<服务器IP>:~")
    print(f"然后在服务器上：tar xzf {out.name} -C /opt && cd /opt/fupan && sudo bash deploy/install.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
