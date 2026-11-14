"""S6-D2：并发缓存回归（保留 S5 的 60/60 口径，不放宽断言）。

做法：把 `tests/test_p0_cache_concurrency.py::test_leader_exception_all_waiters_wake`
作为单次压力用例，分别运行：

- 干净环境 30 次；
- 受控 CPU 负载（默认 8 个忙线程）下 30 次。

任何一次失败都记入 JSON 并让脚本以非 0 退出。断言仍由测试文件本身提供
（`build_count <= 2` 等），本脚本不改测试、不加 skip、不放宽阈值。

用法：
    python scripts/s6_concurrency_regression.py --out reports/s6/performance/concurrency-regression.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CN_TZ = timezone(timedelta(hours=8))
TEST = "tests/test_p0_cache_concurrency.py::test_leader_exception_all_waiters_wake"


def _run_once() -> dict:
    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "--no-header"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return {
        "exit_code": result.returncode,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "tail": (result.stdout or "").strip().splitlines()[-1:] or [""],
    }


def _burn(stop: threading.Event) -> None:
    value = 0
    while not stop.is_set():
        value = (value * 3 + 1) % 1_000_003


def _phase(name: str, runs: int, load_threads: int) -> dict:
    stop = threading.Event()
    threads = [
        threading.Thread(target=_burn, args=(stop,), daemon=True) for _ in range(load_threads)
    ]
    for thread in threads:
        thread.start()
    try:
        records = [_run_once() for _ in range(runs)]
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=2)
    passed = sum(1 for item in records if item["exit_code"] == 0)
    return {
        "name": name,
        "runs": len(records),
        "passed": passed,
        "failed": len(records) - passed,
        "load_threads": load_threads,
        "max_elapsed_ms": max(item["elapsed_ms"] for item in records),
        "failures": [item for item in records if item["exit_code"] != 0],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "reports" / "s6" / "performance" / "concurrency-regression.json"))
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--load-threads", type=int, default=8)
    args = parser.parse_args()

    phases = [
        _phase("clean", args.runs, 0),
        _phase("cpu_load", args.runs, args.load_threads),
    ]
    total = sum(item["runs"] for item in phases)
    passed = sum(item["passed"] for item in phases)
    payload = {
        "commit_sha": subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip(),
        "branch": subprocess.run(
            ["git", "-C", str(ROOT), "branch", "--show-current"], capture_output=True, text=True
        ).stdout.strip(),
        "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "environment": f"Windows 11, Python {sys.version.split()[0]}, 8 线程负载",
        "command": "python scripts/s6_concurrency_regression.py",
        "exit_code": 0 if passed == total else 1,
        "tool_version": "s6_concurrency_regression/1.0",
        "result": {
            "case": TEST,
            "assertion_policy": "不修改断言、不新增 skip；build_count<=2 等阈值由测试文件提供",
            "phases": phases,
            "total_runs": total,
            "total_passed": passed,
            "verdict": f"{passed}/{total}",
        },
        "evidence_files": [args.out],
        "limitations": [
            "这是重复压力测试，不是形式化并发正确性证明。",
            "CPU 负载线程只制造调度竞争，不模拟数据库锁争用。",
        ],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{payload['result']['verdict']} -> {out}")
    return payload["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
