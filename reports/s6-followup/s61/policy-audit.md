# S6.1：Egress 脚本审计（应用前）

- 审计对象：`deploy/s6/egress-deny.nft`、`apply-egress-deny.sh`、
  `verify-egress.sh`、`rollback-egress.sh`、`snapshot-egress-state.sh`
- 审计方式：静态阅读 + `bash -n` 语法检查（**未在任何环境应用规则**）
- 结论：脚本设计满足任务书 §3.3 的边界要求，但**尚未在 VM 内实测**，
  因此 OS 门禁仍为 `NOT_VERIFIED`。

## 1. 语法检查

在 WSL 中仅做语法解析（不执行、不触碰宿主机防火墙）：

| 脚本 | `bash -n` |
| --- | --- |
| `apply-egress-deny.sh` | exit 0 |
| `verify-egress.sh` | exit 0 |
| `rollback-egress.sh` | exit 0 |
| `snapshot-egress-state.sh` | exit 0 |

## 2. 策略审计（`egress-deny.nft`）

| 项 | 结论 |
| --- | --- |
| 默认拒绝 | 独立表 `inet s6_egress`，两条 output 链末尾 `counter drop`，其余全部 drop |
| 不整段放行 loopback | 只精确放行 `127.0.0.1` / `::1` 的 **3128、8000** 端口 |
| DNS | 只放行 `127.0.0.53:53`（UDP/TCP）与 `::1:53`；其它 DNS 目标被 drop |
| IPv4 / IPv6 | 两条链同时覆盖 `ip daddr` / `ip6 daddr` |
| TCP / UDP | 显式端口放行 + 兜底 drop（UDP 除 DNS 外全部拒绝） |
| 代理绕过 | 只允许受控代理端口；`7890` 等常见本机代理端口不被放行 |
| TLS / sendfile / 子进程 | 都在 output 链被同一规则覆盖（不区分 syscall） |
| 原生工具 | curl / python socket / nft 自身都受 uid 规则约束 |
| 幂等 | 规则写入独立表；`apply` 重复执行由脚本的 `nft -f` 覆盖语义保证 |
| 回滚 | `rollback-egress.sh` 只删除 `inet s6_egress` 表，不动宿主机既有规则 |
| 计数器 | 两条链的 `drop` 均带 `counter`，可用于 N24 证明拦截发生在 OS 层 |

## 3. 已知边界（必须在 VM 实测时注意）

1. `meta skuid != "fupan" return`：只覆盖**该 uid** 的进程。若应用以 root 或
   其它 uid 运行，规则不生效 —— VM 内必须以专用低权限用户运行服务；
2. 允许本机 3128 代理：若代理进程本身能出网，则「应用不出网」成立、但
   「机器不出网」不成立；验收口径必须在应用层 + OS 层分别取证；
3. IPv6 无路由 ≠ IPv6 被拦截：必须看 `counter drop` 增量或抓包，不能只看
   「连接失败」；
4. `.invalid` 域名解析失败 ≠ DNS 被拦截：必须确认请求确实到达 resolver 或被 drop。

## 4. 未执行项

- 未在宿主机 / WSL 应用规则（任务书禁止）；
- 未在专用 VM 应用规则（本机 `S61_VM_UNAVAILABLE`，见 `vm-inventory.md`）；
- N01–N24 全部未跑。
