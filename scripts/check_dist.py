"""部署后自检：从首页出发，**真实请求**每个被引用到的前端产物，有 404 就失败。

## 为什么需要它（2026-10-12 线上事故）

`clean_dist.py` 的引用正则只认带前导斜杠的 `/assets/xxx.js`，漏了 vite 另外两种写法
（`assets/xxx.js` 与 `"./xxx.js"`）——而 2026-10-12 那轮把 16 个页面改成按需加载后，
页面 chunk 恰恰是以这两种形式被引用的。结果：所有页面文件被当成旧产物删光，
`index.html` 还在、其它页面全部 404，**登录页都打不开，整站白屏**。
当时的部署后核验只看了 `index.html`，所以没发现。

这道检查**不依赖任何静态解析器的完备性**：它顺着引用关系真的去请求每一层，
所以只要某条引用解析不出来、或对应文件被误删，就会立刻报出来。
正则与 `clean_dist.py` **共用同一组**，避免两边口径漂移。

用法::

    python scripts/check_dist.py                       # 默认查 http://127.0.0.1:8000
    python scripts/check_dist.py --base https://zhifujing.top
"""

import argparse
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

# 与清理脚本共用正则：口径必须一致，否则「清理时认得、自检时不认得」就白搭
sys.path.insert(0, str(Path(__file__).resolve().parent))
from clean_dist import _REF, _REL  # noqa: E402

#: 引用总数上限（正常约 35 个）。只作防呆：万一正则放开过头导致扫描爆炸
MAX_REFS = 300


def _fetch(url: str) -> str:
    # 用 GET 而不是 HEAD：静态资源都是 200，GET 更贴近真实浏览器行为
    request = Request(url, headers={"User-Agent": "fupan-deploy-check"})
    with urlopen(request, timeout=10) as response:  # noqa: S310 - 地址由调用方给定
        return response.read().decode("utf-8", errors="ignore")


def main() -> int:
    parser = argparse.ArgumentParser(description="部署后检查前端产物是否有 404")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="站点根地址")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    pending = [f"{base}/index.html"]
    visited: set[str] = set()
    bad: list[tuple[str, str]] = []

    while pending and len(visited) < MAX_REFS:
        url = pending.pop()
        if url in visited:
            continue
        visited.add(url)
        try:
            text = _fetch(url)
        except HTTPError as exc:
            bad.append((url, f"HTTP {exc.code}"))
            continue
        except URLError as exc:
            bad.append((url, f"连接失败 {exc.reason}"))
            continue
        # 两种写法各自解析：`assets/x.js` 是站点绝对路径；`./x.js` 相对当前文件所在目录
        pending.extend(urljoin(url, f"/assets/{name}") for name in _REF.findall(text))
        pending.extend(urljoin(url, f"./{name}") for name in _REL.findall(text))

    if bad:
        print("前端产物自检失败 —— 以下引用取不到：", file=sys.stderr)
        for url, why in bad:
            print(f"  {why}  {url}", file=sys.stderr)
        print(
            "→ 整站可能白屏（按需加载的页面文件缺失）。"
            "优先检查 clean_dist.py 的引用正则是否漏了新的写法。",
            file=sys.stderr,
        )
        return 1

    print(f"前端产物自检通过：{len(visited)} 个引用全部可访问")
    return 0


if __name__ == "__main__":
    sys.exit(main())
