# S6.1 OS 级 Egress Deny 验证

## 1. 当前状态

**`BLOCKED` / `NOT_RUN`** —— 没有专用隔离 Linux VM，任务书禁止在 Windows 宿主或
WSL 执行防火墙规则。G3/G4 保持 `NOT_VERIFIED`。

机器可读：`verification.json`（`status=BLOCKED`，15 条矩阵全部 `NOT_RUN`）。

## 2. 已完成的准备工作

| 项 | 状态 |
| --- | --- |
| 脚本审计与加固 | PASS（见 `policy-audit.md`） |
| 规则模板默认拒绝 + 双栈 + 精确 loopback | PASS（静态） |
| 快照/回滚/自动回滚脚本 | PASS（静态 + `bash -n`） |
| 计数器驱动的验证矩阵 | PASS（静态） |
| 静态安全测试 | **10 passed** |
| 隔离环境 | **BLOCKED**（无专用 VM） |

## 3. 在专用 VM 中的执行步骤

```bash
# 0) 进入专用 VM（非开发机、非 WSL），确认隔离
uname -a
systemd-detect-virt          # 期望 kvm/qemu/vmware 等，不能是 wsl/none
mountpoint -q /mnt/c && echo "必须先卸载宿主盘"

# 1) 基线
bash deploy/s6/snapshot-egress-state.sh --out reports/s6/s61

# 2) 回滚演练（先于正式验证）
S6_ISOLATED_VM=1 APP_USER=fupan S6_NO_AUTO_ROLLBACK=1 bash deploy/s6/apply-egress-deny.sh
bash deploy/s6/rollback-egress.sh
bash deploy/s6/snapshot-egress-state.sh --out reports/s6/s61 --suffix after-rollback
# 对比 before/rollback 的路由、DNS、iptables 文件

# 3) 正式应用 + 验证
S6_ISOLATED_VM=1 APP_USER=fupan bash deploy/s6/apply-egress-deny.sh
S6_ISOLATED_VM=1 APP_USER=fupan bash deploy/s6/verify-egress.sh \
  --out reports/s6/s61/verification.json

# 4) 若验证失败：立即回滚并保留 verification.json
bash deploy/s6/rollback-egress.sh
```

## 4. 通过标准（全部满足才可写 `S61_OS_EGRESS_VERIFIED`）

1. `verify-egress.sh` 所有 `expected=block` 用例 `verdict=PASS`（失败 + 计数器增加）；
2. `ipv6_tcp_public` 在有 IPv6 路由时必须 PASS；无路由时保持 `NOT_VERIFIED`；
3. `loopback_backend_allowed` PASS，`local_proxy_uncontrolled_port` 被 drop；
4. `allowlist_via_proxy` PASS 且目标来自 `egress-policy.json`；
5. 回滚后 before/rollback 路由与防火墙快照一致；
6. 宿主机防火墙/路由未变化（本项目通过 `host-firewall.before.txt` 对照）。

## 5. 不得声称通过的情形

- 只有 Python wrapper 日志；
- 只有连接失败、没有 nft 计数器增加；
- IPv6 无路由却被写成「IPv6 已阻断」；
- 用 `.invalid` 域名解析失败冒充 DNS Egress 拦截；
- allowlist 被临时放宽后测试成功。
