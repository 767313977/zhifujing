"""S6.1：OS 级 egress 脚本的静态安全断言。

这里**不执行**防火墙规则（本机是 Windows 开发机，任务书禁止），
只验证脚本与规则模板满足以下安全属性：

- 必须显式声明隔离 VM（`S6_ISOLATED_VM=1`），并拒绝 WSL / 宿主盘挂载；
- loopback 只放行受控端口，不能整段 `oifname "lo" accept`；
- IPv4 / IPv6、TCP / UDP 都覆盖，默认 drop 且带 counter；
- 回滚只删除自己的表，不 flush 全量规则；
- 验证脚本必须读 nft 计数器，不能只凭连接失败判通过；
- shell 语法正确（用 Git Bash 的 `bash -n`）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
S6 = ROOT / "deploy" / "s6"


def _text(name: str) -> str:
    return (S6 / name).read_text(encoding="utf-8")


def test_apply_requires_isolated_vm_and_refuses_wsl():
    text = _text("apply-egress-deny.sh")
    assert "S6_ISOLATED_VM" in text
    assert "/proc/version" in text and "microsoft" in text
    assert "/mnt/c" in text and "/mnt/d" in text
    assert "systemd-detect-virt" in text
    assert "flush ruleset" not in text


def test_rollback_only_deletes_own_table():
    text = _text("rollback-egress.sh")
    assert "nft delete table inet s6_egress" in text
    assert "flush" not in text
    assert "microsoft" in text


def test_snapshot_is_read_only_and_hashes_outputs():
    text = _text("snapshot-egress-state.sh")
    for marker in ("ip -brief address", "ip -6 route", "resolv.conf", "iptables-save", "sha256sum"):
        assert marker in text
    assert "nft add" not in text and "iptables -A" not in text


def test_ruleset_precise_loopback_and_dual_stack():
    text = _text("egress-deny.nft")
    # 不能整段放行 loopback
    assert 'oifname "lo" accept' not in text
    # 只放行受控端口
    assert "tcp dport { 3128, 8000 }" in text
    # IPv4 / IPv6
    assert "ip daddr 127.0.0.1" in text
    assert "ip6 daddr ::1" in text
    # TCP / UDP
    assert "tcp dport" in text and "udp dport" in text
    # 默认拒绝 + 计数（用于证明 OS 规则命中，而不是只看连接失败）
    assert "counter drop" in text
    assert text.count("counter drop") >= 2
    # 只作用于应用 uid
    assert 'meta skuid != "fupan" return' in text


def test_verify_reads_counters_and_handles_ipv6_without_route():
    text = _text("verify-egress.sh")
    assert "counter_now" in text
    assert "counter_delta" in text
    assert "FAIL_NO_OS_COUNTER" in text
    assert "NOT_VERIFIED" in text
    assert "ip -6 route show default" in text
    assert "egress-policy.json" in text
    assert "allowlist_via_proxy" in text


def test_policy_file_structure():
    policy = json.loads((S6 / "egress-policy.json").read_text(encoding="utf-8"))
    assert policy["version"] == 1
    assert policy["default"] == "deny"
    assert policy["entries"]
    for entry in policy["entries"]:
        assert entry["host_suffix"]
        assert entry["ports"]
        # 不允许 IP literal 作为允许目标
        assert not entry["host_suffix"].replace(".", "").isdigit()


@pytest.mark.parametrize(
    "script",
    [
        "apply-egress-deny.sh",
        "rollback-egress.sh",
        "snapshot-egress-state.sh",
        "verify-egress.sh",
    ],
)
def test_shell_syntax(script: str):
    candidates = [
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files\Git\bin\bash.exe",
    ]
    bash = next((item for item in candidates if Path(item).exists()), None)
    if bash is None:
        pytest.skip("本机没有 Git Bash，无法做 shell 语法检查")
    result = subprocess.run(
        [bash, "-n", str(S6 / script)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr
