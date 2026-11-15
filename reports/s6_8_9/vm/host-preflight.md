# S6.8 Hyper-V 主机只读预检报告

- 时间：2026-11-15（Asia/Shanghai）
- 宿主机：`HAN-LAPTOP`（XIAOMI REDMI Book Pro 14 2025）
- 原始输出：[host_preflight.txt](./host_preflight.txt)
- 脚本：[deploy/s6/vm/host-preflight.ps1](../../../deploy/s6/vm/host-preflight.ps1)
- 结论：**Hyper-V 管理层 PASS；VM 创建/启动/隔离 NOT_RUN；OS Egress 仍 NOT_VERIFIED**
- 本预检执行时**未创建 VM、未创建虚拟交换机、未修改 Windows 防火墙或网络配置**

> **2026-11-15 15:09 更新**：Ubuntu 24.04.5 ISO 已提供并通过 SHA256 校验，
> `s6-egress-vm` 已创建并启动（4 vCPU / 6 GB / 64 GB Dynamic / Internal `s6-egress`）。
> 当前等待 vmconnect 手动安装 Guest；N01–N24 仍未执行。
> 详见 [vm_create_report.md](./vm_create_report.md)。

## 1. 主机实测

| 项 | 值 |
| --- | --- |
| OS | Windows 11 专业版 Insider Preview 10.0.26320（x64） |
| 物理内存 | 31.43 GB |
| 逻辑处理器 | 14 |
| C: | 952.40 GB，剩余 228.52 GB |
| D: | 950.55 GB，剩余 478.83 GB |
| HypervisorPresent | True |
| vmms | Running / Automatic |
| vmcompute | Running / Manual |
| Get-VMHost | 成功（HAN-LAPTOP） |
| 已有 VM | 0 |
| 现有 vSwitch | 仅 `Default Switch`（Internal，172.26.112.1/24） |
| 现有 NAT | 无 |
| `hypervisorlaunchtype` | Auto |

上传的任务书已确认：`Get-VMHost`、`Get-VMSwitch`、`Get-VMHostSupportedVersion` 可用；
Hyper-V 组件 Enabled；`VirtualizationFirmwareEnabled=False` 等 WMI 字段在
Hypervisor 接管后不能单独作为 BIOS 判据。本报告不再重复修改 BIOS。

## 2. 资源规划（按实测调整）

| 项 | 规划 | 依据 |
| --- | --- | --- |
| VM 名称 | `s6-egress-vm` | 与旧脚本一致 |
| 文件目录 | `D:\s6-vm` | D: 剩余 478.83 GB，且不在仓库/生产目录 |
| Guest OS | Ubuntu Server 24.04 LTS x86_64 | 任务书 |
| Generation | 2 | UEFI |
| vCPU | 4 | 14 逻辑处理器，留足宿主余量 |
| 启动内存 | 6 GB | 宿主 31.43 GB，满足同时运行宿主与 VM |
| 动态内存 | 关闭 | 保证测试可重复 |
| OS 磁盘 | 64 GB 动态 VHDX | D: 空间充足 |
| vSwitch | `s6-egress`（Internal） | 不桥接家庭 LAN |
| 子网 | `172.31.240.0/24` | 与 Default Switch 172.26.112.0/24、WLAN 192.168.31.0/24 无冲突 |
| 网关 | `172.31.240.1` | 宿主 vEthernet |
| NAT | `s6-egress-nat` → 172.31.240.0/24 | 受控出口 |
| Guest IP | `172.31.240.10/24` | 静态，便于 SSH/证据回传 |
| 共享目录/剪贴板 | 不配置 | 隔离要求 |
| 基线检查点 | OS 安装 + 依赖就绪后创建 | 不在安装前创建 |

## 3. ISO 已就绪（2026-11-15 更新）

- 路径：`D:\ISO\ubuntu-24.04.5-live-server-amd64.iso`（4,080,486,400 bytes）；
- 本地重算 SHA256：`97f3d7ffb032c3eb3b23d2c8be9cc76e60c2c1f2c0146ba5ba9fe01cafae0fd8`，
  与用户提供值一致；
- 已按 `create-ubuntu-vm.ps1 -Apply -StartVm` 创建并启动 VM；
- 当前停在 Ubuntu 安装器，等待 vmconnect 手动安装。

创建前搜索记录：`Downloads`、`Desktop`、`C:\vm`、`C:\s6-vm`、`D:\vm`、`D:\iso`、`D:\`
仅有一个非 Ubuntu ISO，因此当时未创建；现已由用户补齐官方 ISO。

## 4. 已生成的脚本（创建已执行，见 `vm_create_report.md`）

| 脚本 | 作用 | 状态 |
| --- | --- | --- |
| `deploy/s6/vm/host-preflight.ps1` | 只读主机预检（本轮已执行） | 已执行 |
| `deploy/s6/vm/create-ubuntu-vm.ps1` | 校验 ISO → 建 Internal vSwitch + NAT → 建 Gen2 VM → 可选启动 | 静态检查通过，未执行 |
| `deploy/s6/vm/verify-vm-isolation.ps1` | 只读核对 VM/交换机/NAT/集成服务/检查点/宿主防火墙未变 | 静态检查通过，未执行 |
| `deploy/s6/vm/invoke-guest-egress-acceptance.ps1` | 通过 SSH 推送 deploy/s6 到 Guest，执行 N01–N24 验收并回传证据 | 静态检查通过，未执行 |
| `deploy/s6/vm/guest/ubuntu-bootstrap.sh` | Guest 内静态 IP、依赖、专用 `fupan` 用户 | 语法检查通过，未执行 |
| `deploy/s6/vm/guest/run-egress-acceptance.sh` | Guest 内 snapshot→apply→verify→N01–N24→（post-reboot）rollback | 语法检查通过，未执行 |
| `deploy/s6/vm/host-dns-probe.py` | 宿主受控 DNS 目标（172.31.240.1:53，UDP/TCP），记录是否收到查询 | 语法检查通过，未执行 |

## 5. 执行顺序（ISO 就绪后）

```powershell
# 1) 预检（已做，可重复）
powershell -File deploy/s6/vm/host-preflight.ps1

# 2) 创建（默认 dry-run；-Apply 才真正创建）
powershell -File deploy/s6/vm/create-ubuntu-vm.ps1 `
  -IsoPath D:\iso\ubuntu-24.04-live-server-amd64.iso `
  -IsoSha256 <官方SHA256> -Apply -StartVm

# 3) 在 vmconnect 里完成 Ubuntu 安装，然后在 Guest 内执行 bootstrap
#    （把 deploy/s6/vm/guest/ubuntu-bootstrap.sh 拷入 Guest，或按 README 逐条执行）

# 4) 只读隔离核对
powershell -File deploy/s6/vm/verify-vm-isolation.ps1 -VmName s6-egress-vm

# 5) Guest 内 N01–N24（阶段 1：apply + verify）
powershell -File deploy/s6/vm/invoke-guest-egress-acceptance.ps1 `
  -VmName s6-egress-vm -GuestAddress 172.31.240.10 -SshKeyPath $env:USERPROFILE\.ssh\s6_vm `
  -Phase apply

# 6) 重启 Guest 后（阶段 2：持久性 + rollback）
powershell -File deploy/s6/vm/invoke-guest-egress-acceptance.ps1 `
  -VmName s6-egress-vm -GuestAddress 172.31.240.10 -SshKeyPath $env:USERPROFILE\.ssh\s6_vm `
  -Phase post-reboot
```

## 6. 仍未验证

- VM 创建/启动/Guest OS：`NOT_RUN`
- 专用 vSwitch / NAT 隔离：`NOT_RUN`
- N01–N24：`NOT_RUN`
- G3/G4：`NOT_VERIFIED`
- 24h Soak：`NOT_RUN`
- Canary：`S6_CANARY_BLOCKED`
