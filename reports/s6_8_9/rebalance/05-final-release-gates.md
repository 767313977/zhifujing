# S6.8/S6.9 平衡方案：最终发布门禁与回滚方案

- 时间：2026-11-15
- 结论：内部开发/合并可用；**有限灰度未批准、生产未授权**；`S6_CANARY_READY` 保持 `BLOCKED`
- 本文件不构成发布授权

## 1. 内部开发/合并门禁

| 条件 | 状态 |
| --- | --- |
| 适用 Release Critical（R1–R10） | PASS（R11 PARTIAL，R12 PENDING） |
| G0 保护资产 before/final | PASS（c0/frozen/db 全一致） |
| 未把 NOT_RUN/NOT_VERIFIED 写成 PASS | 遵守 |
| 生产部署 | 未执行 |

结论：**允许非生产开发/合并**；不得据此生产发布。

## 2. 有限灰度/Canary 申请门禁

| 条件 | 状态 |
| --- | --- |
| R1–R12 适用项 | R1–R10 PASS；R11 PARTIAL；R12 PENDING |
| OLD/NEW 真回滚 | PASS |
| G7 | PASS |
| 应用层 SSRF/出站审查 | PASS_WITH_RESIDUAL_RISKS（`04-application-egress-review.md`） |
| 短时 Smoke | PASS（3 分钟、5918 请求、0 非预期 5xx、5/5 故障注入） |
| 监控/告警/回滚负责人/步骤 | 步骤见 §4；**责任人/告警渠道待指定** |
| 延期风险负责人/影响/缓解/复审 | 见 `03-deferred-risk-register.md`（部分责任人为占位） |
| 独立发布审查 | **PENDING** |
| OS Egress N01–N24 | `NOT_VERIFIED`（必须明示） |

结论：**有限灰度 BLOCKED**，需独立审查与责任人确认。

## 3. 正式生产扩大流量门禁

| 条件 | 状态 |
| --- | --- |
| 24h Staging Soak | `NOT_RUN`（短时 Smoke 不能替代） |
| 全部 Release Critical | R11/R12 未完成 |
| 生产出站控制与真实依赖 allowlist | 未验证（OS Egress DEFERRED） |
| 备份恢复实测 | 仅隔离 Staging 通过；生产未实测 |
| 数据完整性 | 隔离证据通过；生产未变更 |
| 性能 SLO | `THRESHOLD_UNDEFINED`；SQL 53/25 |
| 监控告警 | 未建立 |
| 独立发布批准 | 未取得 |

结论：**生产发布 NOT_AUTHORIZED**。

## 4. 发布与回滚方案（可审计步骤）

### 4.1 发布包与配置

- 代码：`work/s6-8-s6-9-gate-closure` 的最终提交（记录 SHA）；
- 前端：`frontend/dist` 完整 manifest（OLD/NEW 已 SHA256 区分）；
- 依赖：`backend/requirements.txt`、`frontend/package-lock.json`（本轮未升级）；
- 配置：`EXTERNAL_DATA_FETCH_ENABLED=false`、`SCHEDULER_ENABLED=false`、
  `WARMUP_PATTERN_CACHE=false`、`FUPAN_SCHEMA_MODE=verify`、`FUPAN_ENV=staging`；
- 数据库：Research DB 只读；User DB 独立；部署脚本不得自动 migration/create_all。

### 4.2 发布前

1. 只读盘点生产环境；
2. 备份 User DB（backup API），Research DB 做一致性只读副本；
3. 记录 artifact/Git/config SHA；
4. 验证回滚命令可用；
5. 确认无自动迁移/覆盖。

### 4.3 发布步骤

1. 停旧服务（保留上一版本目录与配置快照）；
2. 部署新代码与 `dist`；
3. 启动后检查 `/api/health`、`/api/ready`、readiness 数据库门禁；
4. 登录/刷新/深链/退出 smoke；
5. 核对 C0、冻结信号、Research DB hash、User DB 可写边界；
6. 观察窗口内记录 5xx/延迟/readiness。

### 4.4 回滚步骤

1. 停新服务；
2. 切回上一版本代码 + `dist` + 配置快照；
3. **不自动覆盖数据库**：只有确认 schema 不兼容且已完成备份时才人工恢复；
4. 启动旧版本，检查 health/ready/登录；
5. 复核 Research DB 只读、User DB 完整、保护资产；
6. 记录回滚时间、原因、证据。

### 4.5 立即回滚触发器

- 未授权出站连接成功；
- Research DB 出现写入；
- `PRAGMA integrity_check` 非 `ok`；
- C0/冻结文件/保护资产非预期变化；
- 关键 API 5xx 连续 5 分钟 > 阈值（**THRESHOLD_UNDEFINED**，需发布审查定义）；
- P95 连续 5 分钟 > 灰度前基线 2 倍；
- readiness 持续失败；
- User DB 数据丢失/重复；
- 无法解释的数据质量回归。

### 4.6 责任人/告警（待用户指定）

| 项 | 值 |
| --- | --- |
| 发布负责人 | **待指定** |
| 回滚负责人 | **待指定** |
| 告警渠道 | **待指定** |
| 观察窗口 | 建议 ≥48h（未执行） |
| 回滚时限 | 待发布审查确认 |

## 5. 最终判定

```text
internal_development_merge = PASS (non-production)
limited_canary_application = BLOCKED
production_release = NOT_AUTHORIZED
S6_CANARY_READY = BLOCKED
user_authorization_required = YES
```
