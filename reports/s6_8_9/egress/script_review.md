# S6.8 D1：Egress 脚本与 N01–N24 静态审查

- 审查对象：`deploy/s6/egress-deny.nft`、`apply-egress-deny.sh`、`verify-egress.sh`、
  `rollback-egress.sh`、`snapshot-egress-state.sh`、`scripts/s6_egress_selfcheck.py`
- 方式：静态阅读 + `bash -n` 语法检查（未在任何环境应用规则）
- 结论：脚本设计满足默认拒绝与精确放行；**因无专用 VM，未执行**，G3/G4 保持 `NOT_VERIFIED`

## 1. 只在 VM 内执行的保护

| 检查 | 结论 |
| --- | --- |
| 是否要求隔离环境标记 | `apply/verify/rollback` 均要求 `S6_ISOLATED_VM=1`，否则拒绝执行 |
| 是否会误操作宿主 | 所有命令仅操作独立表 `inet s6_egress`；不 flush 宿主既有规则 |
| 回滚范围 | 只删除 `inet s6_egress` 表，不影响其它表 |
| 快照/恢复 | `snapshot-egress-state.sh` 导出规则与网卡，rollback 前可对照 |
| 幂等 | `nft -f` 覆盖同名表；重复 apply 不叠加 |
| 失败回滚 | apply 失败时由调用方执行 rollback；脚本 `set -euo pipefail` |
| uid 约束 | 规则含 `meta skuid != "fupan" return`，VM 内必须以专用低权限用户运行服务 |

## 2. 规则覆盖（静态）

| 维度 | 结论 |
| --- | --- |
| 默认拒绝 | 独立表两条 output 链末尾 `counter drop` |
| loopback | 只精确放行 `127.0.0.1` / `::1` 的 3128、8000 |
| DNS | 只放行 `127.0.0.53:53`（UDP/TCP）与 `::1:53` |
| IPv4 / IPv6 | 两条链同时覆盖 `ip daddr` / `ip6 daddr` |
| TCP / UDP | 显式放行 + 兜底 drop（UDP 除 DNS 外全部拒绝） |
| 代理绕过 | 7890 等常见本机代理端口不放行 |
| TLS / sendfile / 子进程 / 原生库 | 均在 output 链被同一规则覆盖（不区分 syscall） |
| 计数器 | drop 规则带 `counter`，用于 N24 OS 层证据 |

## 3. N01–N24 定义审查

- 矩阵按仓库既有定义保留 24 项，不删减、不改名、不合并；
- DNS 项（N01/N02）要求**受控目标**，不能用“不存在域名解析失败”冒充阻断；
- IPv6 项（N05/N06）要求 drop counter 或抓包，不能只看“连接失败”；
- N19/N20 覆盖 allowlist 成功与策略缺失 fail closed；
- N21/N22 覆盖幂等与回滚精确性；
- N23 覆盖重启持久性；
- N24 要求 `nft` drop counter 增量，不能只看应用层 wrapper。

## 4. 本轮未执行

- 未在 Windows 宿主应用任何规则；
- 未在 WSL 应用任何规则；
- 无专用 VM（`S61_VM_UNAVAILABLE`），N01–N24 全部 `NOT_RUN`；
- 未把应用层测试结果冒充 OS 层证据。
