"""清理前端构建产物里的旧哈希文件。

**问题**：vite 每次构建都会给产物起一个新哈希名（`index-ByiSmO-O.js` →
`index-Bd3p31Tw.js`）。部署是把 tar 包**解压覆盖**到 `frontend/dist`，所以旧文件
不会被删 —— 每部署一次就多留一套约 1 MB 的 JS/CSS。实测云端积到 26 个文件、
17 MB，而 `index.html` 只引用其中最新的一套。

**做法**：从 `index.html` 出发，顺着引用关系走到不动点，得到「真正被用到的
文件」集合，其余的在 `assets/` 下的一律删除。这叫可达性回收，比「按文件名模式
删」安全 —— 文件名模式（如 `index-*.js`）在 vite 改了输出命名规则后会失效，
而按引用关系判断是**无论怎么改名都成立**的。

**两条安全线**：

1. 一个引用都没解析出来时**直接退出不删**。那说明引用格式变了（改过 `base`、
   换过构建器），此时继续删会把整个站的静态资源删光。
2. 只删 `assets/` 里的文件。`dist/` 根目录下的 `favicon.svg` 之类是 `public/`
   原样拷过来的，名字不参与哈希，不属于旧产物。

用法::

    python scripts/clean_dist.py                 # 清理 frontend/dist
    python scripts/clean_dist.py --dry-run       # 只看会删什么
    python scripts/clean_dist.py --dist /opt/fupan/frontend/dist
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIST = ROOT / "frontend" / "dist"

# 产物之间的引用形式。vite 在默认 base 下产出 `/assets/<名字>`，
# 名字里可能带子目录（配置过 rollup 输出结构时），所以允许斜杠。
# 不在行首锚定：CSS 里是 `url(/assets/x.woff2)`，JS 里是 `"/assets/x.js"`。
_REF = re.compile(r"/assets/([A-Za-z0-9._/-]+)")


def referenced_files(dist: Path) -> set[str]:
    """从 `index.html` 出发，返回被引用到的 assets 文件名（相对 assets 目录）。

    跟着引用**逐层展开**（CSS 里会再引字体/图片、JS 里会再引动态 chunk），
    不是在 index.html 里扫一遍就完 —— 那样会把二级引用当成旧产物删掉。
    """
    entry = dist / "index.html"
    assets = dist / "assets"

    pending = [entry]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        try:
            text = current.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name in _REF.findall(text):
            # 只认 assets 下真实存在的文件：正则也会捞到文档/注释里的示例路径
            target = assets / name
            if name in seen or not target.is_file():
                continue
            seen.add(name)
            pending.append(target)
    return seen


def main() -> int:
    parser = argparse.ArgumentParser(description="清理前端构建产物里的旧哈希文件")
    parser.add_argument("--dist", type=Path, default=DEFAULT_DIST, help="dist 目录")
    parser.add_argument("--dry-run", action="store_true", help="只列出会删什么")
    args = parser.parse_args()

    dist = args.dist.resolve()
    assets = dist / "assets"
    if not assets.is_dir():
        print(f"没有 {assets}，无需清理")
        return 0

    keep = referenced_files(dist)
    if not keep:
        # 兜底见模块开头「两条安全线」第 1 条
        print(
            f"从 {dist / 'index.html'} 没解析出任何 assets 引用，"
            "为安全起见**不清理**（引用格式可能变了，直接删会清空静态资源）",
            file=sys.stderr,
        )
        return 1

    stale = sorted(
        path
        for path in assets.rglob("*")
        if path.is_file() and path.relative_to(assets).as_posix() not in keep
    )
    if not stale:
        print(f"没有旧产物（在用 {len(keep)} 个文件）")
        return 0

    freed = sum(path.stat().st_size for path in stale)
    for path in stale:
        size = path.stat().st_size / 1024
        print(f"  {'[试运行] 将删除' if args.dry_run else '删除'} {path.name}（{size:.0f} KB）")
        if not args.dry_run:
            path.unlink()
    print(
        f"{'将释放' if args.dry_run else '已释放'} {freed / 1024 / 1024:.1f} MB"
        f"，保留 {len(keep)} 个在用文件"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
