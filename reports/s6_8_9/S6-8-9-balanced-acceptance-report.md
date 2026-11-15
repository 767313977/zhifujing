# S6.8 / S6.9 平衡方案（C）验收报告

- 时间：2026-11-15（Asia/Shanghai）
- 仓库分支：`work/s6-8-s6-9-gate-closure`
- 起始 HEAD：`fa2a3ac801bbc032b605e1ad86960da03d433490`（clean，与 origin 同步）
- 执行模式：C — 平衡方案
- 结论：**S6.8 = PARTIAL；S6.9 = PARTIAL（短 Smoke PASS / 24h NOT_RUN）**
- **未合并 master、未部署生产、未启用真实采集、未改变 `S6_CANARY_READY`**

## 1. 结论摘要

| 项 | 状态 |
| --- | --- |
| Release Critical R1–R10 | **PASS** |
| R11 发布回滚方案 | **PARTIAL**（步骤可审计；责任人/告警待指定） |
| R12 独立人工批准 | **PENDING** |
| OLD/NEW 真实版本回滚 | **PASS** |
| G7 | **PASS** |
| G0 保护资产 before/final | **PASS** |
| 应用层 SSRF/出站审查 | **PASS_WITH_RESIDUAL_RISKS** |
| S6.9-A 短时 Smoke | **`S6_9_SHORT_SMOKE_PASS`** |
| S6.9-B 24h Soak | **`SOAK_24H_NOT_RUN`** |
| OS Egress N01–N24 | **`DEFERRED` / `NOT_VERIFIED`** |
| S6_CANARY_READY | **BLOCKED** |

## 2. Release Critical

| ID | 项目 | 状态 | 证据 |
| --- | --- | --- | --- |
| R1 | G0 保护资产 | PASS | `rebalance/protected-assets-before.json`、`gate0-after.json`；c0/frozen/db 全等 |
| R2 | 认证与会话 | PASS | S6.7 报告、E07、loginTransition/routeGuards 测试 |
| R3 | 授权隔离 | PASS | E11 403、E13 双 Context、E14 账号切换 |
| R4 | CSRF/输入边界 | PASS | 全量 pytest、`test_s6_app_egress_ssrf.py`；未放宽认证 |
| R5 | G7 | PASS | 185 路由、readiness 9/9、36 专项测试、迁移幂等/恢复 |
| R6 | 核心浏览器流程 | PASS | E13/E15/E16/三视口 + 部署/重启后登录 smoke |
| R7 | Staging 版本回滚 | PASS | OLD `9838a06` / NEW `a6e9dbd` manifest 不同；serving OLD→NEW→OLD→NEW |
| R8 | 数据库与备份 | PASS | `security/backup-restore.json`：备份可读、丢失可检测、恢复摘要一致 |
| R9 | 全量回归 | PASS | 后端 **1742 passed / 4 skipped**；前端 **168 passed**；lint/typecheck/build 通过 |
| R10 | 依赖与敏感信息 | PASS | pip-audit 0、npm audit 0、敏感扫描 0 |
| R11 | 发布回滚方案 | PARTIAL | `rebalance/05-final-release-gates.md`；责任人/告警待指定 |
| R12 | 最终人工批准 | PENDING | 需独立发布审查 |

## 3. Release Confidence

| 项 | 状态 | 证据 |
| --- | --- | --- |
| E13 双 Browser Context | PASS | 独立 Cookie 罐；A 退出不影响 B；切号无残留；并行刷新不串 |
| E15 Chunk 404 | PASS | 真实 lazy chunk 拦截 2 次；限流自愈 + ErrorBoundary + 恢复 |
| E16 React 渲染异常 | PASS | DEV-only fixture；可见 fallback；其他路由可用 |
| 三视口 1280/1920/390 | PASS | 截图尺寸与视口一致；无横向溢出 |
| SQL 53/25 | `PERF_SQL_BUDGET_NOT_MET` | `performance/sql_profile.json`：53 SQL、40 fingerprint |
| 7 NOT_COMPARABLE | 保留 7 | `data/not_comparable_recheck.md`；无新权威证据 |
| 短时 Smoke | PASS | 5918 请求、0 非预期 5xx、P95 29.78ms、5/5 故障注入 |
| 24h Soak | NOT_RUN | 无连续环境/告警；阈值 `THRESHOLD_UNDEFINED` |
| 故障矩阵 | 短时 5/5 PASS；24h 12 项 NOT_RUN | `soak/short_smoke_fault_matrix.md`、`soak/fault_injection_matrix.md` |

## 4. 延期 OS Egress（Separate Security Workstream）

| 项 | 状态 |
| --- | --- |
| VM | `s6-egress-vm`，**Running**，Gen2/4 vCPU/6 GB/64 GB Dynamic |
| Ubuntu 安装 | **NO** |
| Guest Bootstrap | `NOT_RUN` |
| apply-egress-deny | `NOT_RUN` |
| verify-egress / N01–N24 | `NOT_RUN` |
| `S6_8_DEDICATED_LINUX_VM_READY` | `DEFERRED` |
| `G3_G4_OS_EGRESS_VERIFIED` | `NOT_VERIFIED` |

保留 VM、VHDX、vSwitch、NAT、ISO 与全部证据；**未删除、未强停**。
建议用户确认后正常 `Stop-VM s6-egress-vm` 释放 6 GB 内存。

## 5. 应用层出站 / SSRF

- 出站目标均为固定常量或配置（iFinD/东财/腾讯/同花顺/交易所/飞书等）；
- **未发现用户可控 URL 输入面**；`schemas.py` 的 `url` 是响应字段；
- 应用层默认拒绝 + 域名/端口 allowlist + DNS 闸门 + 代理过滤 + 离线锁；
- **本轮修复**：DNS 解析结果只 pin 公网 IP，阻断私有网段、link-local、CGNAT、
  保留地址与云 metadata（`169.254.169.254`）；7 个新测试通过；
- 残余风险：loopback `allow_any_port=true`、供应商重定向、akshare 内部超时、
  无 OS 层兜底；详见 `rebalance/04-application-egress-review.md` 与风险登记。

## 6. 保护资产

- C0 五脚本 blob：一致；
- 冻结信号 JSONL/meta SHA256：一致；
- 四库 size/mtime/SHA256/integrity：一致；
- before/final：`c0=True`、`frozen=True`、`dbs=True`，`missing=[]`、`integrity_failed=[]`；
- 证据：`rebalance/protected-assets-before.json`、`rebalance/gate0-after.json`。

## 7. 最终门禁

```text
internal_development_merge = PASS（非生产；Release Critical 适用项通过）
limited_canary_application = BLOCKED（R11/R12 + OS Egress NOT_VERIFIED 需独立审查）
production_release = NOT_AUTHORIZED
S6_CANARY_READY = BLOCKED（未自动改变）
user_authorization_required = YES
```

## 8. 未关闭项与风险

见 `rebalance/03-deferred-risk-register.md`：OS Egress、24h Soak、SQL 53/25、
7 条 NOT_COMPARABLE、loopback any-port、VM 资源占用、Insider Preview 宿主、
生产备份恢复、性能阈值未定义、回滚责任人未指定等。

## 9. 证据与 Git

- 统一目录：`reports/s6_8_9/`
  - `rebalance/00–05`
  - `browser/`、`staging/`、`performance/`、`data/`、`security/`、`soak/`、`vm/`
  - `SHA256SUMS`（不含自身与 `push-status.md`）
- 代码改动：`backend/app/external_guard.py`（公网 IP pin）、
  `tests/test_s6_app_egress_ssrf.py`、`scripts/s6_8_backup_restore_drill.py`、
  `scripts/s6_9_resource_monitor.ps1`、`scripts/s7_soak_split.py`（timeline）
- 分支只推送 `work/s6-8-s6-9-gate-closure`；不合并 master、不部署生产。
- 推送状态见 `push-status.md`。

## 10. 停止点

本报告完成后停止，等待 ChatGPT/TRAE 独立审查与用户发布授权；
不自动合并、不自动部署、不自动把 `S6_CANARY_READY` 改为 READY。
