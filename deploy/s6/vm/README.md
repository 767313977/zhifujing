# S6.1 专用 Linux VM 准备（Windows / Hyper-V）

本目录只解决「把独立 Ubuntu VM 准备好」。**OS Egress Deny 的验收仍然在 VM 内执行**
`deploy/s6/` 下的 apply / verify / rollback 脚本。

## 为什么需要独立 VM

- WSL2 共享宿主机内核与网络命名空间，无法证明「规则只作用于该环境」；
- 任务书明确禁止用 Windows 宿主机或日常 WSL 命名空间执行 OS 防火墙规则；
- 需要独立快照、独立虚拟网卡，且不挂载 Windows 用户目录/生产密钥/生产数据库。

## 前置条件

1. 管理员 PowerShell；
2. 允许重启一次宿主机（启用 Hyper-V 是内核级变更）；
3. 自行准备 Ubuntu LTS 的 **VHDX**（脚本不下载镜像；qcow2 需先转换）；
4. VM 需要 ≥ 30GB 虚拟磁盘、2 vCPU、4GB 内存。

## 执行顺序

```powershell
# 1) 启用 Hyper-V（完成后手动重启）
.\prepare-hyperv-vm.ps1 -EnableHyperV

# 2) 重启后：建内部 vSwitch + NAT + VM + 初始检查点
.\prepare-hyperv-vm.ps1 -CreateVm -UbuntuVhdx D:\vm\ubuntu-24.04.vhdx -VmName s6-egress-vm
```

脚本行为：

- 只创建**内部** vSwitch（`s6-internal`）与 NAT（`172.31.60.0/24`），不桥接生产网段；
- 关闭 Guest Service Interface（避免宿主目录共享）；
- 建立 `s6-baseline` 检查点，便于规则出错后回滚；
- **不修改**宿主机防火墙，也不触碰 WSL。

## 验收口径（在 VM 内）

1. 先演练回滚：`bash deploy/s6/rollback-egress.sh`（证明能恢复网络）；
2. 再应用拒绝：`bash deploy/s6/apply-egress-deny.sh`；
3. 执行 `bash deploy/s6/verify-egress.sh`；
4. 按任务书 N01–N24 采集：DNS UDP/TCP、IPv4/IPv6 TCP/UDP、代理、TLS、sendfile、
   子进程、原生工具、allowlist、fail-closed、幂等、回滚、重启持久性、计数器。

证据目录：`reports/s6-followup/s61/`。

## 当前状态

本机 `Microsoft-Hyper-V-All` 为 Disabled，且无 VirtualBox/VMware/QEMU，
因此本轮标记：

```text
S61_VM_UNAVAILABLE
S61_OS_EGRESS_VERIFIED = NOT_VERIFIED
G3 = NOT_VERIFIED
G4 = NOT_VERIFIED
```

详见 `reports/s6-followup/s61/vm-inventory.md`。
