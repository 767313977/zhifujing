# S6.10 最终 Go/No-Go 上线审查报告

- 时间：2026-11-15（Asia/Shanghai）
- 基线：`work/s6-8-s6-9-gate-closure`，审计时 HEAD `6b8a68851b35e68e1924f1705c8c083ac4db77d1`
- 本报告**不构成发布授权**；未执行任何生产部署、merge、调度启用或真实采集开启。

## 0. 最终结论

```text
GO_LIVE_DECISION = NOT_READY
```

**当前不具备正式上线条件**，主要不是代码功能问题，而是：

1. 生产实际配置、数据库模式、备份记录**未被采集**（`NOT_VERIFIED`）；
2. OS 层出站控制 N01–N24 仍未验证（`DEFERRED`）；
3. 服务监控（5xx/延迟/资源）缺失；
4. 上线负责人、值班人、告警渠道、回滚决策人全部 `PENDING`。

代码侧关键项已通过：全量回归、SSRF 收口、核心业务、真实库副本备份恢复、迁移幂等。

---

## A. 技术上线资格

| 维度 | 结果 | 证据 |
| --- | --- | --- |
| 核心功能 | **PASS** | `core-business-acceptance.md`：19 接口 200；浏览器首页/深链/刷新/前进后退 PASS |
| 认证授权 | **PASS** | 登录/退出/真实过期 401/越权 403/跨用户隔离 |
| 数据库 | **PASS（本机）/ NOT_VERIFIED（生产）** | 4 库 integrity ok；G0 前后一致；生产模式未知 |
| 备份恢复 | **PASS（真实库副本）/ NOT_VERIFIED（生产链路）** | `backup-restore-readiness.md`：摘要一致、备份未被覆盖 |
| 回滚 | **PASS（Staging）/ NOT_VERIFIED（生产）** | OLD/NEW 四次真实切换；生产当前版本 SHA 未采集 |
| 安全 | **PASS_WITH_RESIDUAL_RISKS / OS NOT_VERIFIED** | `security/ssrf-final-review.md`；loopback 任意端口已关闭 |
| 监控 | **FAIL（缺口）** | `monitoring-alert-readiness.md`：无 5xx/延迟/资源告警 |
| 回归 | **PASS** | 后端 1751 passed / 4 skipped；前端 168 tests、lint 0 error、build OK |
| 保护资产 | **PASS** | G0 c0/frozen/db 全一致；研究库 SHA256 前后一致 |

```text
technical_go_live_qualification = NOT_QUALIFIED
reason = production evidence missing + OS egress + monitoring
```

## B. 剩余风险

完整清单见 `remaining-risk-register.md`。摘要：

| 级别 | 数量 | 代表项 |
| --- | --- | --- |
| 阻断（上线前必须解决/接受） | 6 | 生产配置未采集、生产 DB 模式未知、OS 出站未验证、生产备份/异地未确认、监控缺口、责任人 PENDING |
| 可接受（需用户明示） | 12 | 24h Soak、12 项故障注入、SQL 53>25、7 条 NOT_COMPARABLE、ready 恒 503、CSRF、子进程出站等 |
| 延期 | 8 | Ubuntu VM 安装、Guest Bootstrap、N01–N24、OS Egress、24h Soak、12 项故障注入、非关键 SQL/架构 |

关键判断：

- `PERF_SQL_BUDGET_NOT_MET (53 > 25)` **原样保留**，本轮只 profiling，未改预算/计数器/语义；
- `REAL_AUCTION_DATA_NOT_VERIFIED` 未关闭 → 竞价采集默认关闭；
- N01–N24 / OS Egress 继续 `NOT_VERIFIED`，不得声明通过。

## C. 人工发布授权

| 项 | 状态 |
| --- | --- |
| 用户明确批准上线 | **PENDING** |
| merge master | **未执行（禁止自动）** |
| 生产部署 | **未执行（禁止自动）** |
| 启用调度 | 生产声明为 `true`（既有配置），本轮未改动；不得由本流程自动开启 |
| 启用真实采集 | **未启用**（竞价默认关闭；采集开关仍受运维控制） |
| 真实用户灰度 | **未开启** |
| `S6_CANARY_READY` | **未修改**（readiness 仍 `BLOCKED`） |

发布授权只能由用户在阅读本报告后**显式给出**。本报告不代替授权。

---

## D. 从 NOT_READY 到 GO 的最小路径

按顺序完成即可重新评审（不需要扩大测试范围）：

1. **生产只读采集**：执行 `deploy/prod_probe.sh`，确认 systemd / nginx / 磁盘 / 数据库模式
   （尤其 `DB_SPLIT_ENABLED`）；回填 `production-config-audit.md` 的 `NOT_VERIFIED` 项。
2. **生产备份核对**：最近 7 天 `db_backup` 日志、备份目录 3 份文件、配置异地目录。
3. **监控最小集**：`/api/health` 黑盒探测、nginx 5xx/P95、磁盘 >85%、采集失败即告警。
4. **指定责任人**：上线负责人、技术值班、告警渠道、回滚决策人/执行人。
5. **OS 出站**：在主机启用 nftables 策略并复核（或由用户书面接受 R-03 风险）。
6. **发布包与回滚包**：记录生产当前 SHA、`cp -a` 上一版目录、生成发布包 SHA256。
7. 重新运行 `deployment-rollback-readiness.md` §5 健康检查清单。

完成 1–4 后可评为 `GO_LIVE_READY_WITH_ACCEPTED_RISKS`（需用户对 R-03/R-07/R-08/R-09 等
明确接受）；完成 5–6 后可评为 `GO_LIVE_READY`。

## E. 本轮实际执行与禁止项核对

| 项 | 结果 |
| --- | --- |
| 先只读审计再修改 | 遵守（`baseline-audit.md`） |
| 每个修改有对应测试 | 遵守（`test_s6_10_*`、SSRF/loopback 用例） |
| 覆盖其他分支成果 | 未覆盖 |
| 擅自扩大范围 | 未扩大 |
| 虚报测试通过 | 未发生；`NOT_RUN/NOT_VERIFIED` 原样保留 |
| 原始证据 | 全部在 `reports/s6_10/` 与 `reports/s6_8_9/` |
| 自动修复生产数据 | 未执行 |
| 使用未授权外部资源 | 未使用 |
| 生产部署 / merge / 灰度 | 未执行 |

## F. 证据索引

| 文件 | 内容 |
| --- | --- |
| `baseline-audit.md` | Git/保护资产/数据库/部署目录基线 |
| `security/ssrf-final-review.md` | SSRF 与出站最终审查 |
| `production-config-audit.md` | 生产/Staging/开发配置分离审计 |
| `database-release-readiness.md` | 数据库与迁移就绪度 |
| `backup-restore-readiness.md` | 备份与恢复就绪度 |
| `deployment-rollback-readiness.md` | 发布与回滚 |
| `core-business-acceptance.md` | 核心业务最终验收 |
| `monitoring-alert-readiness.md` | 监控与应急 |
| `remaining-risk-register.md` | 剩余风险登记 |
| `go-live-decision-report.md` | 本文件 |
| `SHA256SUMS` | 本目录证据校验和 |
| `core-business-acceptance.json` / `backup-restore-realcopy.json` / `browser/history_navigation.json` | 机器可读证据 |
| `backend-regression.log` / `frontend-*.log` / `ssrf-tests.log` / `ruff.log` / `pip-audit.log` | 执行日志 |
| `protected-assets-before.json` / `-final.json` / `gate0-compare.json` | G0 对照 |
