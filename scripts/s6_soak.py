"""S6.4：Staging 短时 soak（明确标注不是 24 小时）。

任务书要求至少 24 小时；本环境无法持续保持 Staging 进程，因此本脚本只做
「有界时间窗」的连续请求与错误率观测，输出 `SOAK_WINDOW_SECONDS`，
**不得**据此写 `S6_STAGING_SOAK_PASSED`。

用途：
- 验证 soak 工具链可用、错误率/延迟可采集；
- 捕捉明显的 5xx、崩溃或锁错误；
- 为后续 24h 运行提供同一口径。

用法：
    python scripts/s6_soak.py --base http://127.0.0.1:8010 \
        --minutes 3 --out reports/s6/s64/soak-metrics.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CN_TZ = timezone(timedelta(hours=8))
USER = "s6staging"
PASSWORD = "staging-password-123"


def _call(opener, url: str, timeout: float = 10) -> tuple[int, float]:
    started = time.perf_counter()
    try:
        with opener.open(url, timeout=timeout) as response:
            response.read(1024)
            return response.status, (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as exc:
        exc.read(1024)
        return exc.code, (time.perf_counter() - started) * 1000
    except Exception:
        return 0, (time.perf_counter() - started) * 1000


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((len(ordered) - 1) * q)))
    return round(ordered[index], 2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8010")
    parser.add_argument("--minutes", type=float, default=3)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--out", default=str(ROOT / "reports" / "s6" / "s64" / "soak-metrics.json"))
    args = parser.parse_args()

    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    login = urllib.request.Request(
        f"{args.base}/api/auth/login",
        data=json.dumps({"username": USER, "password": PASSWORD}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(login, timeout=10) as response:
        if response.status != 200:
            raise SystemExit(f"登录失败：{response.status}")

    endpoints = [
        "/api/health",
        "/api/ready",
        "/api/quality/overview?days=30",
        "/api/quality/timeline?source=limit_pool&days=30",
        "/api/research/workbench?mode=AND&market=all&view=overview&limit=50&offset=0",
        "/api/auction?sort=amount&order=desc&limit=50",
        "/api/sentiment/next-day?days=60&group=first_board&metric=limit_up_count&include_pending=true",
        "/api/limit/promotion?days=15",
    ]
    records: list[dict] = []
    deadline = time.time() + args.minutes * 60
    started_at = datetime.now(CN_TZ).isoformat(timespec="seconds")
    while time.time() < deadline:
        for path in endpoints:
            status, elapsed = _call(opener, f"{args.base}{path}")
            records.append({"path": path, "status": status, "ms": round(elapsed, 2)})
        time.sleep(max(0.0, args.interval))

    by_path: dict[str, dict] = {}
    for record in records:
        bucket = by_path.setdefault(record["path"], {"count": 0, "statuses": {}, "times": []})
        bucket["count"] += 1
        bucket["statuses"][str(record["status"])] = bucket["statuses"].get(str(record["status"]), 0) + 1
        bucket["times"].append(record["ms"])

    all_times = [record["ms"] for record in records]
    total = len(records)
    # /api/ready 在 OS 门禁未验证时**预期**返回 503；这是 readiness 语义，不是服务错误
    ready_503 = sum(
        1 for record in records if record["path"] == "/api/ready" and record["status"] == 503
    )
    errors_5xx = sum(
        1
        for record in records
        if record["status"] >= 500
        and not (record["path"] == "/api/ready" and record["status"] == 503)
    )
    transport_errors = sum(1 for record in records if record["status"] == 0)
    payload = {
        "commit_sha": subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip(),
        "branch": subprocess.run(
            ["git", "-C", str(ROOT), "branch", "--show-current"], capture_output=True, text=True
        ).stdout.strip(),
        "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "environment": f"本地隔离 Staging {args.base}",
        "command": " ".join(["python", "scripts/s6_soak.py", *sys.argv[1:]]),
        "exit_code": 0,
        "tool_version": "s6_soak/1.0",
        "result": {
            "started_at": started_at,
            "duration_minutes": args.minutes,
            "requested_24h": False,
            "status": "SOAK_NOT_24H",
            "total_requests": total,
            "ready_503_expected": ready_503,
            "errors_5xx": errors_5xx,
            "errors_5xx_rate": round(errors_5xx / max(1, total - ready_503), 4),
            "transport_errors": transport_errors,
            "latency_ms": {
                "p50": _percentile(all_times, 0.50),
                "p95": _percentile(all_times, 0.95),
                "p99": _percentile(all_times, 0.99),
                "max": round(max(all_times), 2) if all_times else None,
            },
            "by_path": {
                path: {
                    "count": bucket["count"],
                    "statuses": bucket["statuses"],
                    "p95_ms": _percentile(bucket["times"], 0.95),
                }
                for path, bucket in by_path.items()
            },
            "note": "短时观测不能替代 24h soak；S6_STAGING_SOAK_PASSED 仍为 NOT_RUN。",
        },
        "evidence_files": [args.out],
        "limitations": [
            "不是 24 小时 soak；进程在该窗口外是否稳定未知。",
            "未采集 CPU/内存/文件描述符（本机未安装 psutil）。",
            "空库下部分接口返回 400，属预期数据缺失，不计入 5xx。",
        ],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# S6.4 Staging 短时 soak（NOT 24H）",
        "",
        f"- 目标：`{args.base}`",
        f"- 窗口：{args.minutes} 分钟",
        f"- 请求数：{total}，5xx：{errors_5xx}，传输错误：{transport_errors}",
        f"- P50/P95/P99：{payload['result']['latency_ms']['p50']} / "
        f"{payload['result']['latency_ms']['p95']} / {payload['result']['latency_ms']['p99']} ms",
        "",
        "| 路径 | 次数 | 状态码分布 | P95 |",
        "| --- | --- | --- | --- |",
    ]
    for path, bucket in payload["result"]["by_path"].items():
        lines.append(
            f"| {path} | {bucket['count']} | {bucket['statuses']} | {bucket['p95_ms']} ms |"
        )
    lines.extend(
        [
            "",
            "> **状态：`SOAK_NOT_24H`**。24 小时 soak 未执行，`S6_STAGING_SOAK_PASSED = NOT_RUN`。",
        ]
    )
    (out.parent / "soak-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"soak {args.minutes}min total={total} 5xx={errors_5xx} -> {out}")
    return 0 if errors_5xx == 0 and transport_errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
