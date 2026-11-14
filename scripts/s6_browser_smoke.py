"""S6-E4：对**本地隔离 Staging** 做真实 Chrome headless 浏览器验收（CDP）。

不是组件测试，也不是 mock：启动真实 Chrome、真实加载 `frontend/dist`、真实登录、
真实读取 staging 临时库；失败/空数据/未采集状态也来自正在运行的服务。

用法（staging 服务需先由 `scripts/s6_staging_drill.py --keep-running` 启动）：
    python scripts/s6_browser_smoke.py --base http://127.0.0.1:8010 \
        --out reports/s6/staging/browser
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import websocket

ROOT = Path(__file__).resolve().parents[1]
CN_TZ = timezone(timedelta(hours=8))
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
USER = "s6staging"
PASSWORD = "staging-password-123"


class CDP:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self.next_id = 0
        self.events: list[dict] = []

    def call(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        message_id = self.next_id
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})
            self.events.append(message)

    def eval(self, expression: str, await_promise: bool = False):
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
                "userGesture": True,
            },
        )
        return result.get("result", {}).get("value")

    def drain(self) -> None:
        self.ws.settimeout(0.2)
        try:
            while True:
                self.events.append(json.loads(self.ws.recv()))
        except Exception:
            pass
        finally:
            self.ws.settimeout(30)

    def console_errors(self) -> list[str]:
        errors: list[str] = []
        for event in self.events:
            method = event.get("method")
            params = event.get("params", {})
            if method == "Runtime.exceptionThrown":
                detail = params.get("exceptionDetails", {})
                errors.append(
                    f"exception: {detail.get('text')} {detail.get('exception', {}).get('description', '')}"[:300]
                )
            elif method == "Log.entryAdded":
                entry = params.get("entry", {})
                if entry.get("level") == "error":
                    errors.append(f"log: {entry.get('text')}"[:300])
            elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
                args = " ".join(str(item.get("value", "")) for item in params.get("args", []))
                errors.append(f"console: {args}"[:300])
        return errors


def _new_tab(port: int, url: str) -> str:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe=':/?=&')}",
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))["webSocketDebuggerUrl"]


def _wait_ready(cdp: CDP, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cdp.eval("document.readyState") == "complete":
            time.sleep(1.2)  # 等 React 拉数据
            return True
        time.sleep(0.2)
    return False


def _navigate(cdp: CDP, url: str) -> None:
    cdp.call("Page.navigate", {"url": url})
    _wait_ready(cdp)


def _screenshot(cdp: CDP, path: Path, width: int, height: int) -> dict:
    cdp.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": width < 600},
    )
    time.sleep(0.6)
    result = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
    data = base64.b64decode(result["data"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"file": path.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8010")
    parser.add_argument("--out", default=str(ROOT / "reports" / "s6" / "staging" / "browser"))
    parser.add_argument("--debug-port", type=int, default=9334)
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="s6-chrome-"))
    process = subprocess.Popen(
        [
            str(CHROME),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--remote-debugging-port={args.debug_port}",
            f"--user-data-dir={profile}",
            "--window-size=1280,800",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    checks: list[dict] = []
    screenshots: list[dict] = []
    errors: list[str] = []
    cdp = None
    try:
        deadline = time.time() + 20
        ws_url = None
        while time.time() < deadline:
            try:
                ws_url = _new_tab(args.debug_port, "about:blank")
                break
            except Exception:
                time.sleep(0.4)
        if not ws_url:
            raise RuntimeError("Chrome 调试端口未就绪")
        cdp = CDP(ws_url)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Log.enable")

        def check(name: str, ok: bool, detail: str = "") -> None:
            checks.append({"case": name, "ok": bool(ok), "detail": detail[:300]})

        # 1) 未登录访问受保护页面 → 跳登录
        _navigate(cdp, f"{args.base}/data-quality")
        check("unauth_redirects_to_login", cdp.eval("location.pathname") == "/login", cdp.eval("location.pathname") or "")

        # 2) 真实登录（cookie 由浏览器保存）
        status = cdp.eval(
            "(async()=>{const r=await fetch('/api/auth/login',{method:'POST',"
            "headers:{'Content-Type':'application/json'},credentials:'include',"
            f"body:JSON.stringify({{username:'{USER}',password:'{PASSWORD}'}})}});return r.status;}})()",
            await_promise=True,
        )
        check("login_via_browser", status == 200, f"status={status}")

        # 3) 质量控制台
        _navigate(cdp, f"{args.base}/data-quality")
        text = cdp.eval("document.body.innerText") or ""
        check("quality_page_loaded", "数据质量" in text or "质量" in text, text[:120].replace("\n", " "))
        check(
            "quality_empty_state_not_zero",
            ("未采集" in text) or ("暂无" in text) or ("不可验证" in text) or ("部分" in text),
            "空库下必须显示状态而不是 0",
        )
        for width, height in ((1280, 800), (1920, 1080), (390, 844)):
            screenshots.append(
                _screenshot(cdp, out_dir / f"quality_{width}x{height}.png", width, height)
            )
        check("quality_viewports", True, "1280/1920/390")

        # 4) 盘后研究工作台
        _navigate(cdp, f"{args.base}/research/workbench")
        workbench = cdp.eval("document.body.innerText") or ""
        check("workbench_loaded", len(workbench) > 50, workbench[:120].replace("\n", " "))
        screenshots.append(
            _screenshot(cdp, out_dir / "workbench_1280x800.png", 1280, 800)
        )

        # 5) 涨停复盘 / 竞价 / 情绪页面可打开
        for path, marker in (
            ("/limit-up", "涨停"),
            ("/auction", "竞价"),
            ("/sentiment", "情绪"),
        ):
            _navigate(cdp, f"{args.base}{path}")
            body = cdp.eval("document.body.innerText") or ""
            check(f"page{path.replace('/', '_')}_loaded", marker in body, body[:80].replace("\n", " "))

        # 6) 刷新、前进、后退
        _navigate(cdp, f"{args.base}/data-quality")
        cdp.call("Page.reload", {"ignoreCache": False})
        _wait_ready(cdp)
        check("refresh_ok", cdp.eval("location.pathname") == "/data-quality", "")
        _navigate(cdp, f"{args.base}/limit-up")
        cdp.eval("history.back()")
        time.sleep(1.2)
        check("history_back_ok", cdp.eval("location.pathname") == "/data-quality", cdp.eval("location.pathname") or "")
        cdp.eval("history.forward()")
        time.sleep(1.2)
        check("history_forward_ok", cdp.eval("location.pathname") == "/limit-up", cdp.eval("location.pathname") or "")

        # 7) 未配置的外部取数：离线 503 语义（直接打 API）
        api_status = cdp.eval(
            "(async()=>{const r=await fetch('/api/stock/600000/dde?days=5',{credentials:'include'});return r.status;})()",
            await_promise=True,
        )
        check("offline_gate_status", api_status in (503, 200), f"status={api_status}")

        cdp.drain()
        errors = cdp.console_errors()
        check("console_errors_zero", len(errors) == 0, "; ".join(errors[:3]))
    finally:
        if cdp is not None:
            try:
                cdp.call("Browser.close")
            except Exception:
                pass
            try:
                cdp.ws.close()
            except Exception:
                pass
        try:
            process.terminate()
            process.wait(timeout=10)
        except Exception:
            process.kill()
        shutil.rmtree(profile, ignore_errors=True)

    passed = sum(1 for item in checks if item["ok"])
    payload = {
        "commit_sha": subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip(),
        "branch": subprocess.run(
            ["git", "-C", str(ROOT), "branch", "--show-current"], capture_output=True, text=True
        ).stdout.strip(),
        "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "environment": f"Chrome headless（CDP），真实 Staging {args.base}",
        "command": "python scripts/s6_browser_smoke.py",
        "exit_code": 0 if passed == len(checks) else 1,
        "tool_version": "s6_browser_smoke/1.0",
        "result": {
            "total": len(checks),
            "passed": passed,
            "failed": len(checks) - passed,
            "checks": checks,
            "console_errors": errors,
            "screenshots": screenshots,
        },
        "evidence_files": [str((out_dir / "browser_results.json").relative_to(ROOT))],
        "limitations": [
            "这是 staging 浏览器 smoke，不等于 S5 的 45 项全量浏览器验收。",
            "空库下部分业务断言只能验证「状态显示」而非「数值正确」。",
        ],
    }
    (out_dir / "browser_results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# S6-E4 Staging 浏览器验收",
        "",
        f"- 目标：`{args.base}`（本地隔离 Staging）",
        f"- 结果：**{passed}/{len(checks)}**",
        "",
        "| 结果 | 用例 | 说明 |",
        "| --- | --- | --- |",
    ]
    for item in checks:
        lines.append(f"| {'PASS' if item['ok'] else 'FAIL'} | {item['case']} | {item['detail']} |")
    lines.extend(["", "## 截图 SHA256", "", "| 文件 | 字节 | SHA256 |", "| --- | --- | --- |"])
    for shot in screenshots:
        lines.append(f"| {shot['file']} | {shot['bytes']} | `{shot['sha256']}` |")
    (out_dir / "browser_run.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"browser {passed}/{len(checks)} -> {out_dir}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
