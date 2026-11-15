# S6.8 VM 脚本静态检查

- 时间：2026-11-15
- 说明：本轮只做语法/解析与 dry-run 校验，**未创建 VM、未创建 vSwitch/NAT、未改宿主防火墙**

## 1. 解析检查

| 文件 | 检查 | 结果 |
| --- | --- | --- |
| `deploy/s6/vm/host-preflight.ps1` | PowerShell Parser | OK |
| `deploy/s6/vm/create-ubuntu-vm.ps1` | PowerShell Parser | OK |
| `deploy/s6/vm/verify-vm-isolation.ps1` | PowerShell Parser | OK |
| `deploy/s6/vm/invoke-guest-egress-acceptance.ps1` | PowerShell Parser | OK |
| `deploy/s6/vm/guest/ubuntu-bootstrap.sh` | `bash -n` | OK |
| `deploy/s6/vm/guest/run-egress-acceptance.sh` | `bash -n` | OK |
| `deploy/s6/vm/host-dns-probe.py` | `python -m py_compile` | OK |

## 2. Dry-run

`create-ubuntu-vm.ps1` 默认 **dry-run**。已用一个临时 ISO 校验：
ISO 哈希校验、RAM/磁盘、子网/NAT 冲突、VM 名冲突全部通过，输出
`vm_create_plan.json`，`actions=["DRY_RUN: no switch/NAT/VM created"]`。

- 该临时 ISO **不是** Ubuntu，未用于任何验收结论；
- dry-run 未创建 vSwitch、NAT、VHDX 或 VM；
- 计划文件写在临时目录，未进入证据链。

## 3. 执行进展（2026-11-15 15:09 更新）

| 动作 | 状态 | 说明 |
| --- | --- | --- |
| `create-ubuntu-vm.ps1 -Apply -StartVm` | **PASS** | ISO SHA256 校验通过；VM 创建并启动，退出码 0 |
| 自动检查点纠正 | **PASS** | 关闭自动检查点、删除自动检查点、探针验证后删除；磁盘回到 64 GB Dynamic |
| `verify-vm-isolation.ps1` | **PASS** | 17 项检查全 PASS，退出码 0 |
| `invoke-guest-egress-acceptance.ps1` | NOT_RUN | 等待 vmconnect 手动安装 Guest |
| N01–N24 | NOT_RUN | 同上 |

详见 `vm_create_report.md`。

## 4. 安全边界

- PowerShell 脚本只操作 `inet s6_egress` 之外的 Hyper-V/vSwitch/NAT 资源；
- 未添加任何 Windows 防火墙规则（`verify-vm-isolation.ps1` 会断言 `s6*` 规则数为 0）；
- 未修改宿主代理、DNS、路由；
- Guest 脚本拒绝在 WSL 运行，要求 `S6_ISOLATED_VM=1` 才 apply/verify/rollback。
