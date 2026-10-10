"""前后端接口参数一致性检查（给 CI 用，也可本机直接跑）。

**为什么要有它**：2026-10-11 出过一次这样的 bug —— 前端 `LONG_SYNC_DAYS = 500`，
而后端把 `POST /api/stock/{code}/sync` 的 `days` 上限收到了 250。结果是前端每次
「补长历史」都吃 422、月 K 的均线永远出不来，而**两边各自的检查都发现不了**：
前端 `tsc` 通过、后端 `import app.main` 通过、也没有任何单元测试覆盖这种「契约」。

这个脚本把两边对上：从 FastAPI 的 OpenAPI 里取接口参数的 minimum / maximum，
再从前端源码里扫出**实际调用时写死的常量**，越界就退出码 1。

只覆盖「前端写死、后端有边界」的参数。**新增这类常量时，把它登记到下面的 CHECKS**；
正则匹配不到会报失败（前端改了写法就该来改这里，不要让它静默跳过）。
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.main import app  # noqa: E402  —— 必须放在 sys.path 之后

# (说明, 前端文件, 匹配常量的正则, OpenAPI 路径, 方法, 参数名)
CHECKS = [
    (
        "补长历史一次要几根日线",
        "frontend/src/lib/klinePeriod.ts",
        r"LONG_SYNC_DAYS\s*=\s*(\d+)",
        "/api/stock/{code}/sync",
        "post",
        "days",
    ),
    (
        "个股同步接口的默认值",
        "frontend/src/api/client.ts",
        r"syncStock:\s*\(code: string,\s*days\s*=\s*(\d+)\)",
        "/api/stock/{code}/sync",
        "post",
        "days",
    ),
    (
        "DDE 接口的默认值",
        "frontend/src/api/client.ts",
        r"stockDde:\s*\(code: string,\s*days\s*=\s*(\d+)\)",
        "/api/stock/{code}/dde",
        "get",
        "days",
    ),
]


def _bounds(schema: dict, path: str, method: str, param: str) -> tuple[int | None, int | None]:
    operation = schema["paths"][path][method]
    for item in operation.get("parameters", []):
        if item.get("name") == param:
            spec = item["schema"]
            return spec.get("minimum"), spec.get("maximum")
    raise SystemExit(f"接口 {method.upper()} {path} 上没有参数 {param}（OpenAPI 结构变了？）")


def main() -> int:
    schema = app.openapi()
    failed = 0
    for label, rel, pattern, path, method, param in CHECKS:
        text = (ROOT / rel).read_text(encoding="utf-8")
        found = re.search(pattern, text)
        if not found:
            print(f"[失败] {label}：{rel} 里没匹配到常量（正则得跟着前端改）")
            failed += 1
            continue
        value = int(found.group(1))
        low, high = _bounds(schema, path, method, param)
        if low is not None and value < low:
            print(f"[失败] {label}：前端 {value} < 后端最小值 {low}（{method.upper()} {path}）")
            failed += 1
        elif high is not None and value > high:
            print(
                f"[失败] {label}：前端 {value} > 后端最大值 {high}"
                f"（{method.upper()} {path}）—— 会 422"
            )
            failed += 1
        else:
            print(f"[通过] {label}：{value} 落在 [{low}, {high}]")
    if failed:
        print(f"\n{failed} 项不一致或匹配失败。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
