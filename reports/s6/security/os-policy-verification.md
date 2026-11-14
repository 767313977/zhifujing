# S6-A2 OS 级出站策略验证

## 结论（先行）

| 项 | 状态 | 说明 |
| --- | --- | --- |
| OS 级出站默认拒绝 | **`NOT_VERIFIED`** | 本轮没有可用的隔离 OS / 网络命名空间 / 容器环境执行真实防火墙测试 |
| 规则是否已加载 | **未应用** | 任务书 §A2 明确禁止在用户日常开发机安装/修改全局防火墙 |
| IPv4 / IPv6 覆盖 | **`NOT_VERIFIED`** | 规则文件已同时覆盖 v4/v6，但未在真实主机加载 |
| DNS UDP/TCP 53 | **`NOT_VERIFIED`** | 应用层已拦截 `getaddrinfo`；OS 层未验证 |
| DoH/DoT 替代出口 | **`NOT_VERIFIED`** | 需由 egress proxy / OS 策略统一约束 |
| 子进程继承 | **`NOT_VERIFIED`** | nftables `meta skuid` 可覆盖同 uid 子进程，但未实测 |
| 重启后规则持久性 | **`NOT_VERIFIED`** | 脚本不写 `/etc/nftables.conf`，需生产变更单独审批 |
| 规则加载失败阻止 ready | **PASS（应用层）** | `/api/ready` 在 `EGRESS_OS_POLICY_VERIFIED=NOT_VERIFIED` 时返回 503 |

**因此 G3 / G4 不通过，灰度被阻塞。** 不得把本文件解读为「OS 已隔离」。

## 1. 交付的可执行材料（仅脚本，未执行）

| 文件 | 作用 |
| --- | --- |
| `deploy/s6/egress-deny.nft` | nftables 独立表 `inet s6_egress`：应用 uid 只允许回环 + 受控 proxy + 指定 resolver，其余 drop |
| `deploy/s6/apply-egress-deny.sh` | 备份现有 ruleset → 应用规则；要求 `S6_ISOLATED_ENV=1` |
| `deploy/s6/rollback-egress.sh` | 只删除本方案独立表，不影响主机既有规则 |
| `deploy/s6/verify-egress.sh` | 真实负向测试：直连公网 v4/v6、DNS、UDP/53、curl、经 proxy 的 allowlist 目标 |

## 2. 设计要点（回答任务书 §A2 的 12 个问题）

| 问题 | 本方案的回答 |
| --- | --- |
| 1. 规则由谁加载 | 生产变更由 systemd oneshot 或运维脚本加载；脚本只允许在隔离环境执行 |
| 2. Web 进程启动前是否生效 | 要求 unit 增加 `After=` 依赖防火墙 oneshot；未验证 |
| 3. 重启后是否仍存在 | 需要写入 `/etc/nftables.conf`；脚本明确不自动持久化，避免未审批的持久变更 |
| 4. 加载失败是否阻止 ready | 应用层已做（OS 门禁 NOT_VERIFIED → ready=false）；OS 侧加载失败需由 systemd 依赖保证 |
| 5. 是否覆盖 IPv4/IPv6 | 规则表为 `inet`，同时写 v4/v6；未实测 |
| 6. DNS 53 是否受约束 | 仅放行指定 resolver（示例 `127.0.0.53`），其余 drop |
| 7. DoH/DoT 如何处理 | 应用不主动使用；第三方库若走 DoH 仍受域名策略约束；OS 层需在 proxy 上统一出口 |
| 8. 子进程是否继承 | 同 uid 子进程受 `meta skuid` 约束；`lark-cli` 若换 uid 则不受约束 → 灰度禁用 |
| 9. 容器能否绕过宿主机策略 | 需容器网络策略（NetworkPolicy / 独立 netns）；本轮无容器环境，NOT_VERIFIED |
| 10. 是否有独立审计日志 | nftables `counter drop` 可计数；未接入日志采集 |
| 11. 运维如何核验 | `nft list table inet s6_egress` + `verify-egress.sh` 输出的 JSON |
| 12. 回滚是否短暂开放全网 | 否：只删除独立表，主机既有策略保持不变 |

## 3. 在隔离环境执行的最小步骤（留给下一轮 / 运维）

```bash
# 1) 备份 + 应用（隔离 Staging）
S6_ISOLATED_ENV=1 APP_USER=fupan sudo -E ./deploy/s6/apply-egress-deny.sh

# 2) 验证（结果写 /tmp/s6-egress-verification.json）
S6_ISOLATED_ENV=1 APP_USER=fupan sudo -E ./deploy/s6/verify-egress.sh

# 3) 重启服务与主机，再跑一次 verify，确认规则仍在

# 4) 回滚演练
S6_ISOLATED_ENV=1 sudo -E ./deploy/s6/rollback-egress.sh
```

验收要求：`verify-egress.sh` 中所有 `expected=block` 的用例必须 `verdict=PASS`；
任何一条失败都表示 OS 策略未生效，灰度必须继续阻塞。

## 4. 未验证原因（诚实记录）

1. 本轮环境是 Windows 11 开发机，没有 Linux netns/容器；nftables 无法执行。
2. 任务书禁止在用户日常开发机安装全局防火墙规则。
3. 生产主机与生产网络不可访问，无法确认现有策略、DNS 路径与代理拓扑。
4. 因此本文件的所有 OS 结论均为 `NOT_VERIFIED`，不存在「基本通过」。
