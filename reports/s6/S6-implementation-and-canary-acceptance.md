# S6 实施与灰度验收报告

- 起始 SHA：`c6db475b9848fc37e703be0bd3f186c9d20d053e`
- 分支：`release/s6-canary-hardening-v2`（自上述 HEAD 创建，不改写既有历史）
- 生成时间：2026-11-15（Asia/Shanghai）
- **最终状态：`S6_CANARY_BLOCKED`**
- **生产部署：未执行、未授权。**

## 1. 阶段结果

| 阶段 | 目标 | 结果 | 证据 |
| --- | --- | --- | --- |
| Gate 0 | 基线/环境/保护资产 | **PASS** | `gate0/git-baseline.json`、`environment-isolation.md`、`protected-assets.md` |
| S6.1 | OS 级 Egress Deny | **BLOCKED / NOT_RUN** | `s61/verification.json`、`policy-audit.md` |
| S6.2 | Research/User DB 拆分 | **PARTIAL**（基础层 PASS，G7 未关闭） | `s62/db-access-inventory.md`、`verification.md`、`migration-*.json` |
| S6.3 | 安全回归/启动锁定 | 应用层 **PASS**，OS 层 **NOT_VERIFIED** | `s63/security-regression-matrix.json` |
| S6.4 | soak/依赖安全 | 漏洞扫描 **PASS**；24h soak **NOT_RUN** | `s64/vulnerability-scan.md`、`soak-report.md` |
| S6.5 | 全量回归/浏览器/性能/回滚 | **PASS**（soak 除外） | `s65/regression.md`、`performance.md`、`browser.md` |
| S6.6 | 发布门禁/灰度 | **BLOCKED** | `S6-gate-matrix.json` |

## 2. S6.1 为什么是 BLOCKED

- 本机是 Windows 11 开发机；WSL2 存在，但任务书 §0.3 明确禁止在 WSL 宿主环境执行防火墙；
- 无 Docker、无专用 Linux VM、无独立 network namespace；
- 因此**没有**在宿主/WSL 安装或修改任何防火墙规则；
- 已交付并静态验证：快照、apply、verify、rollback 脚本；规则默认拒绝、双栈、
  精确 loopback（3128/8000/53）、计数器判定、IPv6 无路由标 NOT_VERIFIED、
  自动回滚；`tests/test_s6_egress_scripts.py` 10 passed。

## 3. S6.2 已完成 / 未完成

**已完成**

- `db.py`：只读 Research URL、`PRAGMA query_only=ON`、`ResearchSessionLocal` /
  `UserSessionLocal` / `get_research_db` / `get_user_db` / `user_session_scope`；
- `db_roles.py`：10 张用户表单一事实来源；
- `init_user_db()`：只建用户表，显式调用；
- 迁移工具：dry-run/apply、隔离目录、backup API、幂等、行数/主键摘要/外键核对；
- 真实 513 MB 副本迁移：9 行、FK 0、integrity ok、源 SHA256 不变、二次执行 PASS；
- 只读负向测试与缺库 fail closed。

**未完成（G7 不能关闭）**

- 路由层仍使用混合 `get_db`；8 处跨域服务需应用层双 Session；
- `require_login` 仍写同一库；
- 研究现取写入口未收敛；
- 启动流程未双库校验；
- 文件系统只读权限未落地；
- 拆分默认关闭，`/api/ready` 的 `DATABASE_READ_ONLY_VERIFIED` 仍为 FAIL。

## 4. 安全回归

- 应用层：DNS/代理/socket/TLS/sendfile/重定向/IP literal/策略损坏共 50 条测试 PASS；
- OS 层：15 条矩阵全部 `NOT_RUN`；
- 子进程/原生库：应用层不可覆盖，OS 层未验证；
- 启动顺序：调度器启动时 wrapper 已安装（测试证明）；
- readiness：health 200 / ready 503，`DATABASE_READ_ONLY_VERIFIED=FAIL`。

## 5. 依赖与稳定性

- `pip-audit 2.10.1`：No known vulnerabilities（52 个依赖）；
- `npm audit`（官方 registry）：0 vulnerabilities；
- 许可证：64 个包，29 个未知 → 披露风险；
- soak：2 分钟、1,560 请求、5xx=0；**24h soak 未执行**（`S6_STAGING_SOAK_PASSED=NOT_RUN`）。

## 6. 全量回归

- 后端 **1711 passed / 4 skipped**（358.29 s）；
- Ruff 通过；compileall 通过；`git diff --check` 通过；
- 前端 **154 passed / 16 files**；lint 0/0；`tsc -b` 通过；build 成功；
- 浏览器：5 页 PASS、console 0；发现并修复 AdvanceDeclinePanel 404 空态 loading 不结束；
- 性能：10 板块 **53 SQL / 123.48 ms**，`PERF_SQL_BUDGET_NOT_MET` 保留；
- Staging：部署/重启/回滚/再部署全部 PASS。

## 7. 数据质量

- 7 条 `NOT_COMPARABLE` 保留；
- `THEME_REAL_DATA_ACCEPTANCE_PENDING`、`REAL_AUCTION_DATA_NOT_VERIFIED`、
  `HISTORICAL_DATA_GAP_UNDER_REVIEW` 保留；
- 未把缺失值改成 0，未把未验证数据写成通过。

## 8. 保护资产

- C0 五 blob、冻结信号 SHA256、四库 size/mtime/SHA256/integrity 与 Gate 0 **完全一致**；
- 生产库未写入、未迁移、未替换；
- 详见 `S6-protected-assets-final.json`。

## 9. 未关闭风险

1. **G3/G4**：OS 级出站默认拒绝未验证（无隔离 VM）；
2. **G7**：Research/User DB 路由层未拆分；
3. **24h soak / 崩溃恢复 / 锁争用 / 磁盘满**：NOT_RUN；
4. **生产备份恢复**：NOT_RUN（无生产权限）；
5. **53 SQL > 25**：如实保留；
6. **依赖许可证 29 个未知**；
7. 子进程（`lark-cli`）出口应用层不可控。

## 10. 结论与下一步

不满足 `S6_CANARY_READY`；不得合并发布分支、不得部署生产灰度。

按顺序的最小动作：

1. 提供专用隔离 Linux VM（非 WSL），执行 `deploy/s6/*` 并保存 `verification.json`；
2. 完成 S6.2 路由层拆分与全路径只读测试，再打开 `FUPAN_DB_SPLIT_ENABLED`；
3. 在隔离 Staging 执行 24h soak、崩溃/锁争用/磁盘满注入；
4. 生产备份恢复与流量/监控能力实测；
5. 全部通过后再申请灰度。

**本轮已停止，未合并 master、未部署生产、未启用真实采集。**
