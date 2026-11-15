# S6.8 专用 Linux VM（Windows / Hyper-V）

本目录负责把「独立 Ubuntu VM」准备好并在 Guest 内执行 OS Egress 验收。
**Windows 宿主与 WSL 都不会执行 `apply-egress-deny.sh`。**

## 1. 当前主机状态（2026-11-15）

- Windows 11 专业版 Insider Preview 10.0.26320，31.43 GB RAM / 14 逻辑处理器
- Hyper-V 管理面 PASS：vmms Running / Automatic、vmcompute Running、`Get-VMHost` 可用
- 已有 VM：0；现有 vSwitch：仅 `Default Switch`（Internal，172.26.112.1/24）
- 尚无 Ubuntu 24.04 ISO/VHDX → **VM 创建尚未执行**
- 证据：`reports/s6_8_9/vm/host-preflight.md`、`host_preflight.txt`

## 2. 前置条件

1. 管理员 PowerShell 5.1；
2. Ubuntu Server 24.04 LTS x86_64 ISO（官方来源 + 官方 SHA256）；
3. 目标目录剩余空间 ≥ 74 GB（64 GB 动态 VHDX + 余量）；当前 `D:` 剩余 478.83 GB；
4. 不使用 `Default Switch`，不用 WSL 做验收；
5. 不修改宿主防火墙、代理、DNS 或路由。

## 3. 执行顺序

```powershell
# 0) 只读预检（可重复）
powershell -File deploy/s6/vm/host-preflight.ps1

# 1) 创建（默认 dry-run；-Apply 才真正创建）
powershell -File deploy/s6/vm/create-ubuntu-vm.ps1 `
  -IsoPath D:\iso\ubuntu-24.04-live-server-amd64.iso `
  -IsoSha256 <官方 SHA256> -Apply -StartVm

# 2) 在 vmconnect 完成 Ubuntu 安装；Guest 内执行：
#    sudo bash ubuntu-bootstrap.sh --ssh-pubkey-file /path/to/id_ed25519.pub
#    （把 deploy/s6/vm/guest/ubuntu-bootstrap.sh 拷入 Guest）

# 3) 只读隔离核对
powershell -File deploy/s6/vm/verify-vm-isolation.ps1 -VmName s6-egress-vm -RequireCheckpoint

# 4) Guest 内验收：apply + verify + N01-N24（阶段 1）
powershell -File deploy/s6/vm/invoke-guest-egress-acceptance.ps1 `
  -VmName s6-egress-vm -GuestAddress 172.31.240.10 `
  -SshKeyPath $env:USERPROFILE\.ssh\s6_vm -Phase apply

# 5) 重启 Guest 后：持久性 + rollback（阶段 2）
powershell -File deploy/s6/vm/invoke-guest-egress-acceptance.ps1 `
  -VmName s6-egress-vm -GuestAddress 172.31.240.10 `
  -SshKeyPath $env:USERPROFILE\.ssh\s6_vm -Phase post-reboot
```

## 4. 网络与资源

| 项 | 值 |
| --- | --- |
| vSwitch | `s6-egress`（Internal，不绑定物理网卡） |
| 子网 | `172.31.240.0/24` |
| 宿主网关 | `172.31.240.1` |
| NAT | `s6-egress-nat` |
| Guest | `172.31.240.10/24`，网关/DNS 指向 172.31.240.1 |
| VM | Gen2 / 4 vCPU / 6 GB / 64 GB 动态 VHDX |
| 共享 | 不配置文件共享、剪贴板、宿主目录挂载 |
| 集成服务 | Guest Service Interface 关闭；Enhanced Session 使用 VMBus |

受控 DNS：`host-dns-probe.py` 绑定 `172.31.240.1:53`（UDP+TCP），记录是否收到
Guest 查询。Guest 默认拒绝生效时，宿主端应为 0 条查询，且 nft drop counter 增长。

## 5. N01–N24 证据

`guest/run-egress-acceptance.sh`：

- 每个 block 用例必须同时满足「exit code != 0」与「nft counter 增量 > 0」；
- 只有连接失败、没有 counter 的，记为 `FAIL_NO_OS_COUNTER`，不写成 PASS；
- IPv6 无默认路由时记 `NOT_VERIFIED`；
- N21 幂等用规则表哈希对比；N22/N23 在 post-reboot 阶段验证；
- N20（策略损坏 fail closed）与需要独立快照的项，未自动执行时保持 `NOT_RUN`。

## 6. 禁止项

- 不在 Windows 宿主或 WSL 执行 apply/verify/rollback；
- 不把 `Default Switch` 或宿主防火墙规则当作 Guest OS 证据；
- 不桥接家庭/办公 LAN；
- 不挂载生产库、生产密钥或 Windows 用户目录。

## 7. 当前 Gate

```text
Hyper-V management             PASS
VM create / boot               NOT_RUN（缺 Ubuntu ISO）
Dedicated vSwitch / NAT        NOT_RUN
N01-N24                        NOT_RUN
G3 / G4                        NOT_VERIFIED
24h Soak                       NOT_RUN
S6_CANARY_READY                BLOCKED
```

旧脚本 `prepare-hyperv-vm.ps1` 仅支持 VHDX，保留作历史参考；新流程使用
`create-ubuntu-vm.ps1`（ISO + 资源校验 + dry-run）。
