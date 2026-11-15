# S6.8/S6.9 平衡方案：门禁重新分级

- 时间：2026-11-15
- 依据：`zhifujing_S6.8_S6.9_平衡版调整任务书_TRAE.md`（C — 平衡方案）
- 原则：**缩减范围，不降低真实状态**；`NOT_RUN`/`NOT_VERIFIED`/`DEFERRED` 不改写为 PASS

## 1. Release Critical（必须完成）

| ID | 项目 | 必须达到的结果 | 当前状态 | 证据 |
| --- | --- | --- | --- | --- |
| R1 | G0 保护资产 | before/final 无未解释变化 | **PASS** | `rebalance/protected-assets-before.json`；final 待收尾 |
| R2 | 认证与会话 | 登录/登出/过期/刷新/401 正确 | **PASS** | S6.7 报告、E07、loginTransition 测试 |
| R3 | 授权隔离 | 非管理员拒绝；跨账号不串 | **PASS** | E11、E13、E14、E16 |
| R4 | CSRF/输入边界 | 既有安全测试通过、不放宽 | **PASS** | 全量 pytest（认证/审计用例）、未改认证代码 |
| R5 | G7 | 只读/写边界/readiness/迁移/写路径 | **PASS** | 29 passed、185 路由、9/9、迁移 PASS |
| R6 | 核心浏览器流程 | 登录/首页/研究页/深链/刷新/前进后退/退出 | **PASS** | `browser/` + 本轮补充 |
| R7 | Staging 版本回滚 | OLD/NEW 不同构建，真实切换/回滚/再部署 | **PASS** | `staging/version_switch.json` |
| R8 | 数据库与备份 | integrity、备份可读/可恢复（隔离 Staging） | **PASS** | `security/backup-restore.json`（loss 检测 + restore digest 一致） |
| R9 | 全量回归 | 后端/前端/lint/类型/构建 | **PASS（本轮复跑）** | 1742 passed / 4 skipped；168 passed；lint/build PASS |
| R10 | 依赖与敏感信息 | pip-audit/npm audit/敏感扫描/脱敏 | **PASS（本轮复跑）** | `security/security_scan_rebalance.log` |
| R11 | 发布回滚方案 | 发布包/配置/DB 兼容/回滚步骤/责任人/停止条件 | **PARTIAL**（步骤可审计；责任人/告警待指定） | `rebalance/05-final-release-gates.md` |
| R12 | 最终人工批准 | 独立审查后单独批准 | **PENDING** | 自动化不得自行授权 |

## 2. Release Confidence（建议保留）

| ID | 项目 | 当前状态 | 证据 / 说明 |
| --- | --- | --- | --- |
| C1 | E13 双 Browser Context | **PASS** | 真实 Chrome CDP，两独立 Cookie 罐 |
| C2 | E15 Chunk 404 | **PASS** | 真实 lazy chunk 拦截 2 次；限流自愈 + ErrorBoundary + 恢复 |
| C3 | E16 render error | **PASS** | DEV-only fixture；可见 fallback；其他路由可用 |
| C4 | 三视口 1280/1920/390 | **PASS** | PNG 尺寸与视口一致 |
| C5 | 24h Soak | **NOT_RUN** | 仅做短时 Smoke；不拼接 |
| C6 | 故障注入 | **PARTIAL** | 短 Smoke 覆盖重启/异常退出/DB 只读/缺库/锁争用；磁盘满未做 |
| C7 | SQL 53/25 | **PERF_SQL_BUDGET_NOT_MET** | 保留 53；未改预算/计数器 |
| C8 | 7 NOT_COMPARABLE | **保留 7** | 无新权威证据 |

## 3. Deferred（Separate Security Workstream）

| 项 | 状态 | 原因 |
| --- | --- | --- |
| 专用 Ubuntu VM + 完整 N01–N24 | **DEFERRED** | 高成本纵深防御；本轮不作为发布阻断 |
| `S6_8_DEDICATED_LINUX_VM_READY` | **DEFERRED** | VM 已创建但 Guest 未安装 |
| `G3_G4_OS_EGRESS_VERIFIED` | **NOT_VERIFIED** | 未执行任何 OS 防火墙规则 |
| `S6_9_SOAK_24H_PASS` | **NOT_RUN** | 仅短时 Smoke |

保留：`s6-egress-vm`、VHDX、`s6-egress` vSwitch、NAT、ISO、创建报告、脚本与静态审查。
不删除、不强停；建议用户确认后正常 Stop-VM 释放 6 GB 内存。

## 4. 生产环境仍必须做的应用层出站检查

1. 服务器端 HTTP client / DNS / 子进程 / 上传 / Webhook / 外部 API 出口清单；
2. URL 协议/主机/IP/端口校验，阻断 loopback、link-local、私网、云 metadata、重定向绕过；
3. DNS rebinding、IPv4/IPv6 双栈、重定向后重新校验；
4. 出站 allowlist 与变更审计；
5. 子进程固定可执行文件 + 参数数组；
6. 超时、响应大小、TLS 校验、日志脱敏；
7. 隔离 Staging 用受控目标验证允许/拒绝；
8. 输出应用层出站清单与风险登记（不等价于 OS N01–N24）。

## 5. 判定

```text
内部开发/合并门禁：Release Critical 适用于本次变更的项通过后允许非生产合并
有限灰度/Canary：需 R1-R12（适用项）+ SSRF 审查 + 短 Smoke + 风险登记 + 独立批准
正式生产扩量：24h Soak 或书面风险接受 + 全部 Release Critical + 生产出站策略
S6_CANARY_READY：不由本任务自动改变
```
