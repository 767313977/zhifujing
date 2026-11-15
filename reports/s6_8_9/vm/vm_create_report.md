# S6.8 VM 创建报告（s6-egress-vm）

- 时间：2026-11-15 15:09–15:12（Asia/Shanghai）
- 宿主：`HAN-LAPTOP`（Windows 11 Pro Insider Preview 10.0.26320）
- 分支 / HEAD：`work/s6-8-s6-9-gate-closure` / `95fb6a867c9e8b67805e5d132b5eb81abae0f35e`
- 执行前工作区：clean；本地 HEAD 与 `origin/work/s6-8-s6-9-gate-closure` 一致
- 结论：**VM 创建/启动 PASS**；已暂停，等待 vmconnect 手动安装 Ubuntu
- **未执行 Guest Bootstrap、未执行 N01–N24**

## 1. 前置校验

| 检查 | 结果 |
| --- | --- |
| Git 基线 | clean；HEAD = origin = `95fb6a8` |
| ISO 存在 | `D:\ISO\ubuntu-24.04.5-live-server-amd64.iso`（4,080,486,400 bytes） |
| ISO SHA256（本地重算） | `97f3d7ffb032c3eb3b23d2c8be9cc76e60c2c1f2c0146ba5ba9fe01cafae0fd8` |
| 与用户提供值 | **完全一致** |
| VM 名冲突 | 无（`s6-egress-vm` 不存在） |
| VHDX 冲突 | 无（`D:\s6-vm\s6-egress-vm-os.vhdx` 不存在） |
| vSwitch 冲突 | 无（`s6-egress` 不存在） |
| NAT 冲突 | 无（`s6-egress-nat` 不存在） |
| 子网冲突 | 无（172.31.240.0/24 未被占用） |
| 资源 | 31.43 GB RAM / 14 LP；D: 剩余 475.02 GB |
| 脚本解析 | PowerShell Parser OK；参数与预期一致 |

基线：`pre_create_baseline.json`。

## 2. 执行命令

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\s6\vm\create-ubuntu-vm.ps1 `
  -IsoPath "D:\ISO\ubuntu-24.04.5-live-server-amd64.iso" `
  -IsoSha256 "97f3d7ffb032c3eb3b23d2c8be9cc76e60c2c1f2c0146ba5ba9fe01cafae0fd8" `
  -Apply -StartVm -HeartbeatTimeoutSeconds 90
```

- 退出码：**0**
- 完整日志：`create_vm_execution.log`
- 计划/结果：`vm_create_plan.json`（dry-run 阶段）、`vm_create_result.json`

## 3. 创建结果

| 项 | 实测 |
| --- | --- |
| VM | `s6-egress-vm`，Running，Gen2 |
| vCPU | 4 |
| 启动内存 | 6,442,450,944 bytes（6 GB） |
| 动态内存 | False |
| OS 磁盘 | `D:\s6-vm\s6-egress-vm-os.vhdx`，**Dynamic**，64 GB |
| 当前快照数 | 0（创建能力已通过探针验证，见 §5） |
| 自动检查点 | False（已关闭） |
| 自动启动动作 | Nothing |
| vSwitch | `s6-egress`，**Internal**，不绑定物理网卡 |
| 宿主网关 | `172.31.240.1/24` on `vEthernet (s6-egress)` |
| NAT | `s6-egress-nat` → `172.31.240.0/24` |
| Guest 网卡 | `s6-egress`，MAC `00155D1F2D01`，状态 Ok |
| DVD | Ubuntu ISO，SCSI 0/1 |
| 启动顺序 | DVD → Hard Drive → Network |
| Secure Boot | On，`MicrosoftUEFICertificateAuthority` |
| Guest Service Interface | Disabled（无宿主目录共享） |
| Enhanced Session Transport | VMBus |

## 4. 创建过程中的异常与纠正（如实记录）

首次启动后，Hyper-V 的默认 **AutomaticCheckpointsEnabled=True** 在 15:09:53
自动创建了一个检查点，当前挂载磁盘因此变成 Differencing AVHDX（base 仍是
64 GB Dynamic）。这与“创建后再做 OS 基线快照”的计划不一致。

已按以下顺序纠正（日志：`checkpoint_cleanup.log`）：

1. `Set-VM -AutomaticCheckpointsEnabled $false`；
2. 删除自动检查点（合并回 base VHDX）；
3. 创建 `s6-snapshot-capability-probe` → 确认可创建 → 立即删除（合并）；
4. 复核：磁盘回到 `s6-egress-vm-os.vhdx`（Dynamic / 64 GB），快照数 0，
   自动检查点关闭。

未安装 Guest OS、无用户数据，纠正不涉及数据丢失。快照能力已由“创建-确认-删除”
探针与自动检查点两次实际创建证明。

## 5. 隔离与宿主状态核对

脚本：`verify-vm-isolation.ps1`，退出码 **0**，`vm_isolation.json` 全部 PASS。

| 检查 | 结果 |
| --- | --- |
| VM / Gen2 / 4 vCPU / 6 GB / 动态内存关闭 | PASS |
| 64 GB Dynamic VHDX | PASS |
| Guest Service Interface 关闭 | PASS |
| vSwitch Internal / 不绑定物理网卡 | PASS |
| NAT 前缀 172.31.240.0/24 | PASS |
| 宿主网关 172.31.240.1 | PASS |
| 宿主防火墙无 `s6*` 规则 | PASS（0 条） |

宿主 before/after（`host_state_compare.json`）：

- Windows 防火墙 profiles 状态不变；规则总数 **769 → 769（delta 0）**；
- 新增 vSwitch 仅 `s6-egress`（Internal）；`Default Switch`、WSL 交换机未改；
- WLAN IP 仍为 `192.168.31.45`，未被修改；
- 新增 NAT 仅 `s6-egress-nat`（预期）；
- 未修改 WSL 网络配置（仅只读执行 `wsl -l -v`）。

## 6. 当前状态与暂停点

```text
VM = s6-egress-vm / Running / Gen2 / 4 vCPU / 6 GB / 64 GB Dynamic
Boot = DVD first（Ubuntu ISO 已挂载）
Guest OS = 未安装（等待 vmconnect 手动安装）
Guest Bootstrap = NOT_RUN
N01-N24 = NOT_RUN
G3/G4 = NOT_VERIFIED
```

**下一步（由用户执行）**：打开 `vmconnect.exe localhost "s6-egress-vm"` 完成
Ubuntu Server 24.04 安装（安装期无 DHCP，可离线安装；安装后按 README 执行
`guest/ubuntu-bootstrap.sh` 配置 172.31.240.10/24）。

## 7. 证据

- `pre_create_baseline.json`、`post_create_state.json`、`host_state_compare.json`
- `vm_create_result.json`、`create_vm_execution.log`
- `checkpoint_cleanup.log`
- `verify_vm_isolation.log`、`vm_isolation.json`
- `host-preflight.md` / `host_preflight.txt`（创建前主机预检）
