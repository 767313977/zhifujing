# S6.8 C 包：专用 Linux VM 盘点与隔离

- 时间：2026-11-15（Asia/Shanghai）
- 结论：**`S61_VM_UNAVAILABLE`**
- 本轮未创建/启动任何 VM，未修改 BIOS/UEFI，未重启宿主，未在宿主或 WSL 修改防火墙

## 1. 只读盘点

| 项 | 值 | 判定 |
| --- | --- | --- |
| Windows Edition | Windows 11 专业版 Insider Preview（10.0.26320） | 支持 Hyper-V（需启用） |
| CPU | Intel Core Ultra 5 225H | 支持虚拟化的型号 |
| `VirtualizationFirmwareEnabled` | **False** | **固件未开启 VT-x**；启用需改 BIOS/UEFI（本轮禁止） |
| `SecondLevelAddressTranslationExtensions` | **False** | SLAT 未报告，进一步说明固件虚拟化未启用 |
| `Microsoft-Hyper-V-All` | **Disabled** | 无 Hyper-V 管理面 |
| `VirtualMachinePlatform` | Enabled | 仅 WSL2 使用，不等于独立 VM |
| `HypervisorPlatform` | Enabled | 同上 |
| `Get-VM` | 未安装 | 无 Hyper-V PowerShell 模块 |
| VBoxManage / vmrun / qemu-system-x86_64 / virsh | 均未安装 | 无第三方虚拟化 |
| 现有专用 Linux VM | 无 | — |
| 快照能力 | 不可用 | 依赖 Hyper-V 检查点 |
| 网络隔离能力 | 不可验证 | 无独立 vSwitch/网卡 |
| WSL2 | Ubuntu-24.04（Stopped） | **禁止**作为 OS 验收环境 |

## 2. 为什么不能在本轮补齐

1. 固件虚拟化（VT-x/SLAT）未启用，单纯“启用 Hyper-V 功能”不足以创建 VM；
2. 修改 BIOS/UEFI 需要用户本人操作，任务书明确禁止本轮修改；
3. 启用 Hyper-V 通常需要重启宿主，任务书明确禁止在用户工作期间自动重启；
4. 无 VirtualBox/VMware/KVM，且不得安装未知来源软件或购买云资源；
5. WSL2 共享宿主内核/网络命名空间，不能证明独立网卡、独立快照与规则仅作用于该环境。

## 3. 已准备的工具（静态审查通过，未执行）

- `deploy/s6/vm/prepare-hyperv-vm.ps1`（UTF-8 BOM，创建 VM/独立 vSwitch/关闭共享剪贴板）
- `deploy/s6/vm/README.md`（provisioning、快照、N01–N24 回填流程）
- `deploy/s6/egress-deny.nft` + `apply/verify/rollback/snapshot` 脚本
- `reports/s6_7_login/n01_n24_execution_matrix.md`（每项命令、预期、证据）

## 4. 解除条件（需用户授权）

1. 在 BIOS/UEFI 启用 Intel VT-x（及 SLAT），保存并重启；
2. 管理员 PowerShell 启用 `Microsoft-Hyper-V-All` 并重启；
3. 按 `deploy/s6/vm/README.md` 创建 Ubuntu VM（独立 vSwitch、无共享目录/剪贴板）；
4. 在 VM 内做快照，再执行 `snapshot → apply → verify → N01–N24 → rollback`。

G3/G4 在完成上述步骤前保持 `NOT_VERIFIED`。
