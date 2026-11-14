# S6.1：OS Egress 验证状态

**状态：`S61_VM_UNAVAILABLE` / `NOT_VERIFIED`**

| 项 | 结果 |
| --- | --- |
| 是否应用过防火墙规则 | 否（宿主机、WSL、VM 均未应用） |
| 是否修改宿主机防火墙 | 否 |
| 是否把 WSL 当作验收环境 | 否 |
| 脚本语法检查 | 4/4 PASS（`bash -n`） |
| 策略静态审计 | 见 [policy-audit.md](./policy-audit.md) |
| N01–N24 | 全部 `NOT_RUN` |
| `S61_OS_EGRESS_VERIFIED` | `NOT_VERIFIED` |
| G3 / G4 | `NOT_VERIFIED` |

机器可读：[verification.json](./verification.json)。

## 为什么没有执行

本机：

- `Microsoft-Hyper-V-All` = Disabled；
- 无 VirtualBox / VMware / QEMU；
- 仅存在 WSL2 Ubuntu-24.04，按任务书**禁止**作为 OS 防火墙验收环境。

启用 Hyper-V 需要管理员权限 + 重启宿主机，属于改变宿主运行形态，本轮不擅自执行。

## 交付

- `deploy/s6/vm/prepare-hyperv-vm.ps1`（启用 Hyper-V、建隔离网络与 VM、建检查点）
- `deploy/s6/vm/README.md`（前置条件与验收口径）
- `reports/s6-followup/s61/vm-inventory.md/.json`

## 不能据此宣称的内容

- ❌ 「OS 层默认拒绝已验证」
- ❌ 「IPv6 已被拦截」（无路由 ≠ 被拦截）
- ❌ 「应用层 network_attempts=0 等于 OS 层通过」
