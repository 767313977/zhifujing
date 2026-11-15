# S6.8 专用 Linux VM 预检（只读，本轮不执行防火墙变更）

- 生成时间：2026-11-15（Asia/Shanghai）
- 主机：Windows 10 Pro（XIAOMI REDMI Book Pro 14 2025，x64）
- 结论：**`S61_VM_UNAVAILABLE`**；G3/G4 继续保持 `NOT_VERIFIED`
- 本轮未修改宿主 / WSL 防火墙、代理、DNS 或路由

## 1. 环境盘点（只读）

| 项 | 结果 | 说明 |
| --- | --- | --- |
| Microsoft-Hyper-V-All | Disabled | 需要管理员权限 + 重启才能启用 |
| Microsoft-Hyper-V / Hypervisor / Services / Tools | Disabled | 管理面不可用 |
| VirtualMachinePlatform | Enabled | 由 WSL2 使用，不等于独立 VM 能力 |
| HypervisorPlatform | Enabled | 同上 |
| WSL2 发行版 | Ubuntu-24.04 | **禁止**作为 OS 防火墙验收环境（共享宿主内核/网络命名空间） |
| VBoxManage / vmrun / qemu-system-x86_64 | 未安装 | 无第三方虚拟化 CLI |
| 独立 VM | 无 | 未发现可用 Hyper-V/KVM/VMware 虚拟机 |
| 快照能力 | 不可用 | 依赖 Hyper-V 检查点；当前功能未启用 |
| 管理员权限 | 未在本轮申请/使用 | 未做任何系统级变更 |
| 网络隔离能力 | 不可验证 | 无独立 vSwitch / 独立网卡 |

完整机器可读记录见 `reports/s6-followup/s61/vm-inventory.json`（上一轮采集，环境未变化）。

## 2. 已准备的工具（静态审查通过，未执行）

| 文件 | 用途 | 状态 |
| --- | --- | --- |
| `deploy/s6/vm/prepare-hyperv-vm.ps1` | 创建/启动 Ubuntu VM、配置独立 vSwitch、禁用共享剪贴板/目录 | 静态审查通过；本机未执行 |
| `deploy/s6/vm/README.md` | provisioning、快照、N01–N24 证据回填流程 | 已复核 |
| `deploy/s6/egress-deny.nft` | nftables 默认拒绝 + 精确 loopback/DNS 放行 | 规则审计见 `s61/policy-audit.md` |
| `deploy/s6/apply-egress-deny.sh` | 幂等应用规则 | `bash -n` 通过 |
| `deploy/s6/verify-egress.sh` | 按矩阵验证出口 | `bash -n` 通过 |
| `deploy/s6/rollback-egress.sh` | 只删除 `inet s6_egress` 表 | `bash -n` 通过 |
| `deploy/s6/snapshot-egress-state.sh` | 应用前快照 | `bash -n` 通过 |

## 3. 启用 VM 的前置条件（不在本轮执行）

1. 管理员 PowerShell 启用 `Microsoft-Hyper-V-All` 并重启宿主；
2. 准备 Ubuntu LTS cloud image（脚本不自动下载）；
3. VM 使用**独立 vSwitch**，不得桥接生产网段；
4. VM 内不得挂载 Windows 用户目录、生产密钥、生产数据库；
5. 服务以专用低权限用户（如 `fupan`）运行，nft 规则按该 uid 生效；
6. 先做快照，再执行 `snapshot → apply → verify → rollback`；
7. 证据回填 `reports/s6-followup/s61/verification.json` 后才可改判 G3/G4。

## 4. 本轮未做与禁止项

- 未在 Windows 宿主应用任何防火墙规则；
- 未在 WSL 修改 nftables/iptables；
- 未修改宿主代理或 DNS；
- 未把应用层 503 冒充 OS Egress 证据；
- 未把 `NOT_VERIFIED` 改成 PASS。
