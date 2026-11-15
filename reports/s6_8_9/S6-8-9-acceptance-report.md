# S6.8 / S6.9 实施与门禁验收报告

- 基线分支 / HEAD：`fix/s6-7-login-spa-transition` / `a6e9dbda939051b38b822571db0ba003addf40d0`
- 工作分支：`work/s6-8-s6-9-gate-closure`
- 起始 HEAD：`a6e9dbda939051b38b822571db0ba003addf40d0`
- 代码提交：`6d556e4`（chunk 自愈限流 + DEV fixture）、`c22378b`（CDP/版本化/剖析工具）
- 生成时间：2026-11-15（Asia/Shanghai）
- **结论：S6.8 = PARTIAL（A/B 完成，C/D 阻塞）；S6.9 = BLOCKED（SOAK NOT_RUN）**
- **`S6_CANARY_READY` 仍为 BLOCKED；未合并 master、未部署生产、未启用真实采集**

## 1. 状态词

| 状态词 | 结果 |
| --- | --- |
| `S6_8_BROWSER_RESIDUALS_PASS` | **PASS** |
| `S6_8_STAGING_VERSION_ROLLBACK_PASS` | **PASS** |
| `S6_8_DEDICATED_LINUX_VM_READY` | **未达成（`S61_VM_UNAVAILABLE`）** |
| `S6_8_OS_EGRESS_N01_N24_PASS` | **未达成（N01–N24 全 NOT_RUN）** |
| `G3_G4_OS_EGRESS_VERIFIED` | **NOT_VERIFIED** |
| `S6_9_SOAK_24H_PASS` | **未达成（`SOAK_24H_NOT_RUN`）** |
| `S6_9_FAULT_MATRIX_PASS` | **未达成（NOT_RUN）** |

保留不变：`PERF_SQL_BUDGET_NOT_MET`、7 条 `NOT_COMPARABLE`、
`THEME_REAL_DATA_ACCEPTANCE_PENDING`、`REAL_AUCTION_DATA_NOT_VERIFIED`、
`HISTORICAL_DATA_GAP_UNDER_REVIEW`、`PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED`。

## 2. A 包：浏览器残余项（PASS）

- 工具：`scripts/s6_8_cdp_e2e.mjs`（Node 24 WebSocket + Chrome DevTools Protocol，
  真实 Chrome；未引入 Playwright/Cypress，未改 package.json/lockfile）
- 被测构建 manifest：`082da7f1bcc8ece9d9bbf102073d95a7fccf2432fe5891ac80f7e9e36a568876`

| 场景 | 结果 | 证据 |
| --- | --- | --- |
| E13 双 Context | PASS | A=管理员、B=普通；A 退出不影响 B；A 切 B 后自选为空；并行刷新身份不串 |
| E15 Chunk 404 | PASS | 真实 `Research-*.js` 被拦截 2 次；首次自愈重载，第二次显示 ErrorBoundary；恢复后正常 |
| E16 渲染异常 | PASS | DEV-only fixture；可见 fallback；`/login` 仍可用 |
| 三视口 1280/1920/390 | PASS | 登录/首页/导航/退出/错误 fallback 均可见，无横向溢出；截图尺寸与视口一致 |

证据目录：`browser/`（JSON + 11 张 PNG + `browser_residuals.md`）。

## 3. B 包：真实版本 Staging 回滚（PASS）

- 独立 worktree：OLD `9838a06`、NEW `a6e9dbd`；同一 lockfile（SHA `14fad376…`）
- OLD manifest：`b74306ce53c38148d5690a0adfd9e8913aef17eea2378131411e5d18e1072d6c`
- NEW manifest：`8eb65c3c9976b6b1531499b7b6f0a9d4b2b6b87b8785edcf6bceb867e8a1cb26`
- 实际 serving 的 entry 哈希：OLD `5aacf7a7…` ⇄ NEW `2c72e60b…`，四次切换均核对一致；
- 顺序：OLD → NEW → rollback OLD → redeploy NEW；
- OLD 登录：按已知缺陷复现白屏（`/login`、root=0），作为版本确实切换的对照；
- NEW 登录/刷新/深层链接/退出 smoke：PASS；
- 临时库 SHA 前后一致（`b52ea01c…`），备份可用；
- 证据：`staging/version_switch.json`、`versioned-rollback-report.md`、`build-record.json`、
  `manifest-old/new.json`、`logs/build-*.log`、`login-*.json`。

## 4. C 包：Hyper-V 管理面 PASS；VM 创建 NOT_RUN

> 2026-11-15 追加：用户提供了新的主机预检结果，Hyper-V 管理面已启用。
> 本节以 `vm/host-preflight.md` 为准；旧的 `vm_inventory.md` 是上一时刻快照。

| 项 | 结果 |
| --- | --- |
| Hyper-V 组件 / 服务 | **Enabled；vmms Running/Automatic、vmcompute Running** |
| `Get-VMHost` | PASS（HAN-LAPTOP） |
| HypervisorPresent | True；`hypervisorlaunchtype=Auto` |
| 已有 VM | 0 |
| 现有 vSwitch | 仅 `Default Switch`（Internal，172.26.112.1/24） |
| 现有 NAT | 无 |
| CPU WMI 三个 False | 不能单独解释为 BIOS 未开启；Hypervisor 已接管，Intel 规格支持 VT-x/EPT |
| 主机资源 | 31.43 GB RAM / 14 LP；D: 剩余 478.83 GB |
| Ubuntu ISO | **未找到**（仅找到一个非 Ubuntu ISO） |
| VM 创建/启动 | **NOT_RUN**（按任务书 ISO 来源+SHA256 停止条件） |
| N01–N24 | **NOT_RUN** |
| 结论 | **VM 尚未创建；G3/G4 仍 `NOT_VERIFIED`** |

已生成（未执行创建）：`deploy/s6/vm/create-ubuntu-vm.ps1`、
`verify-vm-isolation.ps1`、`invoke-guest-egress-acceptance.ps1`、
`guest/ubuntu-bootstrap.sh`、`guest/run-egress-acceptance.sh`、`host-dns-probe.py`。
静态检查：`vm/scripts_static_check.md`。

证据：`vm/host-preflight.md`、`vm/host_preflight.txt`、`vm/host-preflight.json`、
`vm/vm_snapshot_before.json`。

## 5. D 包：OS Egress 与 N01–N24（NOT_VERIFIED）

- 静态审查 + `bash -n`：四脚本 exit 0；默认拒绝/双栈/TCP-UDP/代理/TLS/子进程/原生路径覆盖完整；
- **未在任何环境应用防火墙规则**；N01–N24 全部 `NOT_RUN`；
- `firewall_before/after`：`NOT_COLLECTED`（无 VM）；
- G3/G4 保持 `NOT_VERIFIED`；
- 证据：`egress/script_review.md`、`verification.json`、`n01_n24_results.json`、
  `firewall_before.txt`、`firewall_after.txt`、`os_egress_report.md`。

## 6. E 包：24h Soak（NOT_RUN）

前置条件中 D 未通过，按任务书**不启动计时**：

```text
SOAK_24H_NOT_RUN
continuous_hours = 0
```

未把 S6.4 的 3 分钟短窗拼成 24h；故障矩阵 12 项全部 `NOT_RUN`。
证据：`soak/soak_timeline.jsonl`、`metrics_summary.json`、`fault_injection_matrix.md`、
`incident_log.md`、`soak_24h_report.md`。

## 7. F 包：性能与数据差异

### SQL 剖析（只读，未优化）

- workbench：**53 SQL**、40 个唯一 fingerprint、总耗时 218.8 ms；
- 分类：fixed 34 条 / per_sector 19 条 / cache 0 / permission 0；
- 预算 25 未达成：`PERF_SQL_BUDGET_NOT_MET` 保留；
- 未改查询/缓存/预算；优化前置条件（语义、PIT、事务、跨用户缓存隔离 + 等价性测试）已记录；
- 证据：`performance/sql_profile.json`、`sql_profile.md`。

### 7 条 NOT_COMPARABLE

- 逐条复核原始登记与 v4 口径；无新增权威证据；
- **7 条原样保留**，未改分类、未删分母、未把缺失写 0；
- 证据：`data/not_comparable_recheck.md`。

## 8. G 包：全量回归 / G7 / 安全

| 项 | 结果 |
| --- | --- |
| 后端 `pytest -q` | **1735 passed / 4 skipped**（382.96s，未新增 skip） |
| Ruff | All checks passed |
| compileall | 通过 |
| 前端 `npm test` | **168 passed / 20 files**（+4 chunkReload） |
| oxlint | 0 warning / 0 error（139 files） |
| `tsc -b && vite build` | 成功（vite 8.3.8） |
| G7 | 185 路由矩阵；readiness 9/9；29 passed；迁移幂等/恢复 PASS |
| 依赖扫描 | pip-audit 无已知漏洞；npm audit 0 |
| 敏感扫描 | diff 与证据目录 0 命中 |

日志：`logs/backend_test.log`、`frontend_test.log`、`lint_typecheck_build.log`、
`g7_regression.log`、`security_scan.log`、`sensitive_scan.log`、`browser_e2e.log`。

## 9. Gate 0 保护资产

- C0 五脚本：一致
- 冻结信号 JSONL/meta：一致
- 四库 size/mtime/SHA/integrity：一致
- before/final：`c0=True`、`frozen=True`、`dbs=True`、`missing=[]`、`integrity_failed=[]`
- 证据：`gate0_before.json`、`gate0_after.json`

## 10. 未关闭项

| 项 | 状态 |
| --- | --- |
| G3/G4 OS Egress | `NOT_VERIFIED`（无 VM） |
| 24h Soak / 12 项故障矩阵 | `NOT_RUN` |
| PERF | `PERF_SQL_BUDGET_NOT_MET`（53 > 25） |
| DATA | 7 条 `NOT_COMPARABLE` 保留 |
| 生产部署 / 灰度 | 未授权、未执行 |
| S6.8 整体 | **PARTIAL**（A/B PASS，C/D 阻塞） |
| S6.9 整体 | **BLOCKED**（前置 D 未通过） |

## 11. Git / 证据

- 分支：`work/s6-8-s6-9-gate-closure`
- 代码提交：`6d556e4`、`c22378b`；证据提交与推送状态见 `push-status.md`
- 未 amend、未 force push、未合并 master、未部署生产
- 证据目录：`reports/s6_8_9/`；校验：`SHA256SUMS`（不包含自身与 `push-status.md`）

## 12. 解除条件（按顺序）

1. 提供 Ubuntu Server 24.04 LTS ISO 与官方 SHA256（Hyper-V 管理面已 PASS，无需再改 BIOS）；
2. 执行 `deploy/s6/vm/create-ubuntu-vm.ps1 ... -Apply -StartVm` 创建独立 VM
   （`s6-egress` Internal vSwitch + `s6-egress-nat` + 172.31.240.0/24）；
3. Guest 安装后执行 `ubuntu-bootstrap.sh`，再跑 `verify-vm-isolation.ps1`；
4. 用 `invoke-guest-egress-acceptance.ps1` 在 Guest 内执行 N01–N24（apply / post-reboot 两阶段）；
5. G3/G4 改判后，才启动 24h Soak + 12 项故障注入；
6. 全部通过后仅形成灰度申请材料，不自动部署。
