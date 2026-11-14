# S6 Gate 0：环境身份与隔离检查

## 1. 结论（先行）

**当前环境不满足 S6.1 的隔离前置条件，OS 级防火墙验证不得执行。**

- 宿主是日常 Windows 11 开发机（`HAN-LAPTOP`）；
- 已安装 WSL2 `Ubuntu-24.04`（版本 2，Stopped），但任务书 §0.3 明确禁止在
  **WSL 宿主环境**执行防火墙规则，因此不能把 WSL 当作 S6.1 的隔离环境；
- 没有 Docker / 独立 Linux VM / 独立网络命名空间可用于验证；
- 因此 S6.1 状态为 `BLOCKED`，G3/G4 保持 `NOT_VERIFIED`；
- 本轮**没有**在宿主或 WSL 安装/修改任何防火墙规则。

## 2. 探测证据

| 项 | 实测 |
| --- | --- |
| 宿主 OS | Windows 11 10.0.26340 |
| 虚拟化环境 | WSL2 存在：`Ubuntu-24.04`，State=Stopped，Version=2 |
| Docker | 未安装（`docker` 不是可识别命令） |
| 宿主 shell 权限 | 管理员=True（仅用于读取状态，未改防火墙） |
| 宿主防火墙 | Domain/Private/Public 三档 State=ON（基线见 `reports/s6/s61/host-firewall.before.txt`） |
| WSL 网络 | 若启动：`eth0` + 默认路由 `172.28.32.1`，resolver `10.255.255.254`；属于 WSL NAT，**不是**专用隔离 VM |
| /mnt/c、/mnt/d | WSL 自动挂载宿主盘（含生产数据库与配置）→ 违反「不挂载生产数据库/密钥/配置」 |

## 3. 为什么不能直接用 WSL

任务书 §0.3 原文：「不在日常 Windows 开发机、WSL 宿主环境或共享宿主机直接执行防火墙规则；
OS 级验证只能在专用隔离 Linux VM / 网络命名空间中执行。」

WSL2 虽然内核独立，但：

1. 它运行在日常开发机上，属于 WSL 宿主环境；
2. 自动挂载 `/mnt/c`、`/mnt/d`，可访问生产库与配置；
3. 默认 NAT 路由可到达宿主 LAN，无法证明「到生产网段无路由」。

因此本轮拒绝在 WSL 执行 `nft`/`iptables` 变更，也没有安装 nftables。

## 4. 需要的隔离环境（供后续执行）

满足以下全部条件才可运行 `deploy/s6/*`：

1. 专用 Linux VM（非开发机、非 WSL）或独立网络命名空间；
2. 不挂载生产数据库、生产密钥、生产配置；
3. 到生产网段无路由（或明确隔离策略）；
4. 防火墙规则作用域仅限该 VM/namespace；
5. 有可验证回滚通道（VM 控制台 / 快照）；
6. 已保存路由、DNS、IPv4/IPv6、nftables/iptables、代理基线。

建议的最小流程：

```bash
# 在专用 VM 内（root）
cd /opt/s6
bash deploy/s6/snapshot-egress-state.sh --out reports/s6/s61
bash deploy/s6/apply-egress-deny.sh
bash deploy/s6/verify-egress.sh --out reports/s6/s61/verification.json
bash deploy/s6/rollback-egress.sh
bash deploy/s6/snapshot-egress-state.sh --out reports/s6/s61 --after-rollback
```

## 5. 宿主未被修改的证据

- `reports/s6/s61/host-firewall.before.txt`：`netsh advfirewall show allprofiles state`
  与 `route print -4/-6` 基线；
- 本轮没有执行 `netsh advfirewall set`、`New-NetFirewallRule`、`nft`、`iptables`；
- WSL 未启动任何防火墙变更（仅做过只读工具探测，安装 nftables 的请求被用户拒绝）。
