"""探测同花顺 iFinD MCP 服务可用工具清单。

iFinD 的实际工具清单因账号权益而异，官方文档只是通用版。
本脚本用 IFIND_AUTH_TOKEN（来自环境变量或项目根目录 .env）拉取真实清单。

用法:
    python scripts/probe_ifind.py                          # 列出所有服务的工具
    python scripts/probe_ifind.py stock index              # 只列指定服务
    python scripts/probe_ifind.py --call stock search_stocks '{"query":"电子行业市值前10"}'
"""

import json
import os
import sys
import time
import urllib3
from pathlib import Path

import requests

urllib3.disable_warnings()

BASE = "https://api-mcp.51ifind.com:8643/ds-mcp-servers"
SERVERS = {
    "stock": "hexin-ifind-ds-stock-mcp",
    "fund": "hexin-ifind-ds-fund-mcp",
    "edb": "hexin-ifind-ds-edb-mcp",
    "news": "hexin-ifind-ds-news-mcp",
    "bond": "hexin-ifind-ds-bond-mcp",
    "global_stock": "hexin-ifind-ds-global-stock-mcp",
    "index": "hexin-ifind-ds-index-mcp",
    "future": "hexin-ifind-ds-futures-mcp",
}

ROOT = Path(__file__).resolve().parent.parent
_sessions: dict[str, str] = {}
_ids: dict[str, int] = {}


def load_token() -> str:
    token = os.getenv("IFIND_AUTH_TOKEN")
    if token:
        return token
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("IFIND_AUTH_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("缺少 IFIND_AUTH_TOKEN（环境变量或项目根目录 .env）")


TOKEN = load_token()


def _next_id(server_type: str) -> int:
    _ids[server_type] = _ids.get(server_type, 0) + 1
    return _ids[server_type]


def _headers(server_type: str) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": TOKEN,
    }
    if server_type in _sessions:
        headers["Mcp-Session-Id"] = _sessions[server_type]
    return headers


def _parse_body(text: str) -> object:
    """响应可能是纯 JSON，也可能是 SSE 帧，两种都要能解。"""
    text = text.strip()
    if not text:
        return None
    if text.startswith("{") or text.startswith("["):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if payload and payload != "[DONE]":
                return json.loads(payload)
    raise ValueError(f"无法解析响应: {text[:200]!r}")


def _post(server_type: str, payload: dict, timeout: int = 60):
    url = f"{BASE}/{SERVERS[server_type]}"
    resp = requests.post(
        url, json=payload, headers=_headers(server_type), verify=False, timeout=timeout
    )
    return resp, _parse_body(resp.text)


def _init(server_type: str) -> None:
    if server_type in _sessions:
        return
    payload = {
        "jsonrpc": "2.0",
        "id": _next_id(server_type),
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "1.0.0"},
        },
    }
    resp, _ = _post(server_type, payload, timeout=30)
    resp.raise_for_status()
    session_id = resp.headers.get("Mcp-Session-Id")
    if not session_id:
        raise RuntimeError("initialize 未返回 Mcp-Session-Id")
    _sessions[server_type] = session_id
    requests.post(
        f"{BASE}/{SERVERS[server_type]}",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=_headers(server_type),
        verify=False,
        timeout=10,
    )


def list_tools(server_type: str) -> list[dict]:
    _init(server_type)
    payload = {
        "jsonrpc": "2.0",
        "id": _next_id(server_type),
        "method": "tools/list",
        "params": {},
    }
    resp, data = _post(server_type, payload)
    resp.raise_for_status()
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(json.dumps(data["error"], ensure_ascii=False))
    return (data or {}).get("result", {}).get("tools", []) or []


def call_tool(server_type: str, tool_name: str, params: dict, timeout: int = 90) -> dict:
    _init(server_type)
    payload = {
        "jsonrpc": "2.0",
        "id": _next_id(server_type),
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": params},
    }
    resp, data = _post(server_type, payload, timeout=timeout)
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(json.dumps(data["error"], ensure_ascii=False))
    return data or {}


def _flatten(result: dict) -> str:
    """MCP 返回 content 数组，抽出文本部分。"""
    parts = []
    for item in (result.get("result") or {}).get("content") or []:
        if isinstance(item, dict):
            parts.append(item.get("text") or json.dumps(item, ensure_ascii=False))
    return "\n".join(parts) if parts else json.dumps(result, ensure_ascii=False)


def run_call(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    server_type, tool_name, raw = argv[0], argv[1], argv[2]
    if server_type not in SERVERS:
        raise SystemExit(f"未知 server_type: {server_type}，可选 {list(SERVERS)}")

    # PowerShell 会吞掉命令行参数里的双引号，复杂 JSON 用 @文件 传参
    if raw.startswith("@"):
        params = json.loads(Path(raw[1:]).read_text(encoding="utf-8"))
    else:
        params = json.loads(raw)

    print(f">>> {server_type}.{tool_name}  {json.dumps(params, ensure_ascii=False)}")
    started = time.monotonic()
    try:
        result = call_tool(server_type, tool_name, params)
    except Exception as exc:  # noqa: BLE001 - 探测脚本需要展示原始错误
        print(f"[FAIL] {type(exc).__name__}: {str(exc)[:400]}")
        return 1
    cost = time.monotonic() - started
    text = _flatten(result)
    print(f"<<< 耗时 {cost:.1f}s，返回 {len(text)} 字符")
    print(text[:4000])
    if len(text) > 4000:
        print(f"... (已截断，完整返回 {len(text)} 字符)")
    return 0


def main() -> int:
    if "--call" in sys.argv:
        idx = sys.argv.index("--call")
        return run_call(sys.argv[idx + 1:])

    targets = [a for a in sys.argv[1:] if a in SERVERS] or list(SERVERS)
    total = 0
    for server_type in targets:
        try:
            tools = list_tools(server_type)
            total += len(tools)
            print(f"\n=== {server_type} ({len(tools)} 个工具) ===")
            for tool in tools:
                desc = (tool.get("description") or "").replace("\n", " ")[:110]
                print(f"  - {tool.get('name'):<28} {desc}")
        except Exception as exc:  # noqa: BLE001 - 探测脚本需要继续跑完其他服务
            print(f"\n=== {server_type} ===\n  [FAIL] {type(exc).__name__}: {str(exc)[:160]}")
    print(f"\n合计可用工具: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
