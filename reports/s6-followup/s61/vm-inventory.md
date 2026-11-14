# S6.1：专用 Linux VM 与 OS Egress Deny — 虚拟化盘点

- 采集时间：2026-11-15
- 结论：**`S61_VM_UNAVAILABLE`** —— 本机没有可用的独立虚拟机，也没有已启用的
  Hyper-V；WSL2 按任务书**不得**作为 OS 防火墙验收环境。
- G3 / G4：保持 **`NOT_VERIFIED`**（不伪造 OS 层通过）。

## 1. 宿主机

| 项 | 值 |
| --- | --- |
| WindowsProductName | Windows 10 Pro |
| WindowsVersion | 2009（22H2 内核） |
| OsArchitecture | 64-bit |
| Manufacturer / Model | XIAOMI / REDMI Book Pro 14 2025 |
| HypervisorPresent | True（WSL2 的虚拟机平台，不是可用的 Hyper-V 管理面） |

## 2. 虚拟化能力

| 检查 | 结果 |
| --- | --- |
| `Microsoft-Hyper-V-All` | **Disabled** |
| `Microsoft-Hyper-V` / `Hypervisor` / `Services` / `Tools` | **Disabled** |
| `VirtualMachinePlatform` | Enabled（仅支撑 WSL2） |
| `HypervisorPlatform` | Enabled（仅支撑 WSL2 / 第三方 VBS） |
| `VBoxManage` | 未安装（命令不存在） |
| `vmrun`（VMware） | 未安装（命令不存在） |
| `qemu-system-x86_64` | 未安装（命令不存在） |
| `wsl --status` | Ubuntu-24.04（默认版本 2）—— **禁用于本任务验收** |

## 3. 判定

- 启用 Hyper-V 需要管理员权限 + **重启宿主机**，属于改变宿主运行形态；
  本轮不擅自执行，也不把「重启后的未知状态」写成通过。
- WSL2 共享宿主机内核与网络命名空间，无法证明「独立网卡 / 独立快照 /
  OS 防火墙只影响该 VM」，因此不能冒充 S6.1 的隔离环境。
- 因此本轮：

```text
S61_VM_UNAVAILABLE
S61_OS_EGRESS_VERIFIED = NOT_VERIFIED
G3 = NOT_VERIFIED
G4 = NOT_VERIFIED
```

## 4. 已交付的准备工作

| 文件 | 用途 |
| --- | --- |
| `deploy/s6/vm/prepare-hyperv-vm.ps1` | 启用 Hyper-V → 建隔离内部 vSwitch + NAT → 建 2 vCPU/4GB/30GB Ubuntu VM → 建检查点 |
| `deploy/s6/vm/README.md` | 前置条件、执行顺序、隔离要求与验收口径 |

## 5. 下一位执行者需要满足的条件

1. 管理员 PowerShell + 允许重启一次（启用 Hyper-V）；
2. 一份 Ubuntu LTS cloud image（20.04/22.04/24.04 均可，脚本不联网下载）；
3. 能接受「VM 不挂载 Windows 用户目录 / 不接生产网段」；
4. 在 VM 内执行 `deploy/s6/rollback-egress.sh` 预演 → `apply-egress-deny.sh` →
   `verify-egress.sh`，按任务书 N01–N24 采集证据。
