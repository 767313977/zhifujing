# S6.8 D 包：OS Egress 默认拒绝与 N01–N24

- 状态：**`NOT_VERIFIED`**（`S61_VM_UNAVAILABLE`）
- 本轮未在 Windows 宿主或 WSL 应用/修改任何防火墙规则
- 静态审查：`script_review.md`（`bash -n` 全部 exit 0）
- 结果：`verification.json`、`n01_n24_results.json`（N01–N24 全 `NOT_RUN`）
- 规则快照：`firewall_before.txt` / `firewall_after.txt`（`NOT_COLLECTED`）

## 为什么不能改判

任务书要求：
1. 在专用、可快照、与宿主隔离的 Linux VM 内执行；
2. 每项有命令、exit code、stdout/stderr、网络层证据（drop counter/抓包等）；
3. DNS 必须用受控目标，不能以“不存在域名解析失败”代替；
4. 子进程/原生路径必须有 OS 层证据。

当前宿主固件虚拟化未启用（VT-x/SLAT=False），Hyper-V 未启用，无
VirtualBox/VMware/KVM；WSL2 被任务书禁止作为 OS 验收环境。因此：

```text
G3 = NOT_VERIFIED
G4 = NOT_VERIFIED
S61_OS_EGRESS_VERIFIED = NOT_VERIFIED
```

## 已完成的替代证据（不冒充 OS 层）

- 应用层 offline gate：`EXTERNAL_DATA_FETCH_ENABLED=false`，E2E/Staging 仅监听
  `127.0.0.1`；
- 脚本静态审查与语法检查；
- N01–N24 命令、预期、证据 schema 已准备（`n01_n24_results.json`）。

这些**不能**作为 G3/G4 通过依据。
