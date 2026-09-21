"""调 iFinD 官方 skill 的命令行入口（`~/.trae-cn/skills/ifind-finance-data`）。

为什么要有这个：skill 自带的 `call.py` / `call-node.js` 是**模块**，没有命令行入口，
每次取数都要临时写一个脚本、用完再删。这个包装器给它们补一个稳定的入口，
于是「期货 / 工商企业 / 法智法律」这三个 Trae 里没接的服务也能一条命令直接问。

注意：**每次调用都消耗 iFinD 权益次数**（只有 `services` 不消耗，因为走的是 list_tools），
而项目后台的定时采集走同一个账号配额，两边共用。

用法::

    python scripts/ifind_skill.py services                    # 列出 10 个服务与各自的工具（不耗次数）
    python scripts/ifind_skill.py services future             # 只看某个服务
    python scripts/ifind_skill.py call future future_quotes '{"query":"螺纹钢主力连续近5日的涨跌幅"}'
    python scripts/ifind_skill.py call law legal_article_search @params.json

参数里的 JSON 太长时写成文件用 `@文件路径` 传（PowerShell 会吞引号，复杂 JSON 必用这种方式）。
"""

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_SKILL_DIR = Path.home() / ".trae-cn" / "skills" / "ifind-finance-data"
# 服务名 → 一句话说明，顺便当参数校验的白名单
SERVICES = {
    "stock": "A股：选股、行情、财务、股东、风险、ESG、事件",
    "fund": "公募基金：选基、净值、持仓、持有人结构、基金公司",
    "bond": "债券：基本信息、行情、财务、信用债/可转债特殊指标",
    "global_stock": "港股美股：选股、资料、行情、财务、公告事件",
    "index": "指数与板块：行情、板块行情、成分股",
    "edb": "宏观与行业经济指标",
    "news": "财经新闻、上市公司公告、热点事件",
    "future": "期货期权：合约资料、日频行情、持仓与技术指标",
    "enterprise": "工商企业：工商信息、股东股权、司法风险、知识产权、企业筛选",
    "law": "法律：司法案例、法条法规检索与法规全文",
}


def load_skill(skill_dir: Path):
    """把 skill 的 call.py 挂进 sys.path 后导入。"""
    if not (skill_dir / "call.py").exists():
        raise SystemExit(
            f"没找到 skill：{skill_dir}\n"
            f"用 IFIND_SKILL_DIR 指定安装位置，或先安装 ifind-finance-data 技能包"
        )
    sys.path.insert(0, str(skill_dir))
    import call  # noqa: PLC0415 - 依赖上面对 sys.path 的修改，只能在这里导入

    return call


def _loads(text: str) -> object:
    """宽松解析。

    iFinD 用**真实制表符**填充空单元格（非交易日的涨跌幅/持仓量），严格模式下
    `json.loads` 会报 `Invalid control character` —— 必须 strict=False 才解得开。
    """
    return json.loads(text, strict=False)


def render(payload: object) -> str:
    """把 MCP 响应压成可读文本。

    嵌套三层：`result.content[].text` → 里面又是一段 JSON 字符串 → 再里面的
    `data` 还可能是 JSON 字符串。逐层剥开，剥不动就原样给出。

    注意顺序：**先处理 dict，最后才处理字符串**。反过来写的话，递归进来的
    JSON 文本会被「已是字符串就返回」那条提前挡掉，下面的解析永远走不到。
    """
    if isinstance(payload, dict):
        if isinstance(payload.get("content"), list):
            texts = [
                item["text"]
                for item in payload["content"]
                if isinstance(item, dict) and item.get("text")
            ]
            return render("\n".join(texts))
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if not isinstance(payload, str):
        return str(payload)

    text = payload.strip()
    try:
        parsed = _loads(text)
    except json.JSONDecodeError as exc:
        # 解析不了就原样给出，但要带上原因，别让人以为这就是完整结果
        return f"{text}\n\n[提示] 响应不是合法 JSON（{exc.msg}），以上为原文"

    if isinstance(parsed, dict) and isinstance(parsed.get("data"), str):
        try:
            parsed["data"] = _loads(parsed["data"])
        except json.JSONDecodeError:
            pass
    return _format(parsed)


def _format(parsed: object) -> str:
    """有 `answer`（Markdown 表格）就直出表格，其余字段附在后面。

    自然语言类工具把结果塞在 `answer` 里，直接 json.dumps 会把它转义成一整行、
    表格完全没法看。注释里带上 `indicators_params` 这类元信息，方便核对口径。
    """
    if not isinstance(parsed, dict):
        return json.dumps(parsed, ensure_ascii=False, indent=2)

    data = parsed.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("answer"), str):
        return json.dumps(parsed, ensure_ascii=False, indent=2)

    # iFinD 有时把换行写成字面量 \n，还原成真换行表格才立得起来
    body = data["answer"].replace("\\n", "\n").strip()
    rest = {key: value for key, value in data.items() if key != "answer"}
    if not rest:
        return body
    return f"{body}\n\n{json.dumps(rest, ensure_ascii=False, indent=2)}"


def cmd_services(call, only: str | None) -> int:
    targets = [only] if only else list(SERVICES)
    for name in targets:
        result = call.list_tools(name)
        if not result.get("ok"):
            print(f"[{name}] 失败: {json.dumps(result.get('error'), ensure_ascii=False)}")
            continue
        tools = ((result.get("data") or {}).get("result") or {}).get("tools") or []
        print(f"[{name}] {SERVICES[name]}")
        for tool in tools:
            desc = (tool.get("description") or "").split("\n")[0][:70]
            print(f"    {tool.get('name'):<26} {desc}")
    return 0


def cmd_call(call, server: str, tool: str, raw: str) -> int:
    params = json.loads(Path(raw[1:]).read_text(encoding="utf-8")) if raw.startswith("@") else json.loads(raw)
    result = call.call(server, tool, params)
    if not result.get("ok"):
        print(f"[失败] HTTP {result.get('status_code')}")
        print(json.dumps(result.get("error") or result.get("raw"), ensure_ascii=False, indent=2))
        return 1
    print(render((result.get("data") or {}).get("result")))
    return 0


def main() -> int:
    # Windows 控制台默认不是 UTF-8，中文取数结果会乱码
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="iFinD skill 命令行入口")
    parser.add_argument(
        "command", choices=["services", "call"], help="services=列出服务与工具，call=取数"
    )
    parser.add_argument("args", nargs="*", help="call 时为 <服务> <工具> '<JSON参数>'")
    parsed = parser.parse_args()

    skill_dir = Path(os.getenv("IFIND_SKILL_DIR", DEFAULT_SKILL_DIR))
    call = load_skill(skill_dir)

    if parsed.command == "services":
        only = parsed.args[0] if parsed.args else None
        if only and only not in SERVICES:
            raise SystemExit(f"未知服务 {only}，可选：{list(SERVICES)}")
        return cmd_services(call, only)

    if len(parsed.args) != 3:
        raise SystemExit("用法：call <服务> <工具> '<JSON参数>'")
    server, tool, raw = parsed.args
    if server not in SERVICES:
        raise SystemExit(f"未知服务 {server}，可选：{list(SERVICES)}")
    return cmd_call(call, server, tool, raw)


if __name__ == "__main__":
    raise SystemExit(main())
