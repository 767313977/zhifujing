# S6.12 未解决问题与上线后事项

- 时间：2026-11-15

## 1. 上线前必须由用户授权执行

> 已于 2026-11-15 18:18–18:23 执行生产备份、RPS 迁移、部署与重启，服务已恢复。
> 当前剩余：登录后功能验收由用户本人完成。详见 `08-production-upgrade-report.md`。

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 生产 RPS 迁移 | **DONE** | 18:18–18:20 执行，4 列已加、幂等、integrity ok |
| 生产部署 | **DONE** | 18:21 完成，发布包 SHA256 匹配 |
| 重启 `fupan.service` | **DONE** | active，PID 3476546 |
| 核心功能验收（需真实账号） | **PENDING** | 用户本人登录验收 |
| 真实采集检查 | **PENDING** | 部署后观察 15:05 / 次日 09:15 |
| 旧版目录快照（生产侧） | **DONE** | `/opt/fupan-prev-20261115-1800`（345 文件） |

## 2. 已知未解决（不阻断本次最小上线）

| 项 | 状态 | 影响 | 计划 |
| --- | --- | --- | --- |
| 生产版本不可识别 | `PRODUCTION_VERSION_UNIDENTIFIED` | 无法用 commit 回滚；已用完整文件副本兜底 | 上线后给部署包加版本标记 |
| 异地备份 | **未配置** | 磁盘故障会同时丢主库与备份 | 上线后配置（用户已同意暂缓多地域） |
| 监控/告警渠道 | `ALERT_CHANNEL_PENDING` | 5xx/资源/延迟无主动告警 | 上线后接云监控/黑盒探测 |
| OS 出站控制 | `NOT_VERIFIED` | 生产 `OUTPUT ACCEPT`；目标版本应用层 guard 部署后生效 | N01–N24 延期 |
| 24h Soak | `NOT_RUN` | 长时稳定性未验证 | 上线后观察/补测 |
| SQL 预算 | `PERF_SQL_BUDGET_NOT_MET (53 > 25)` | workbench 查询偏多 | 上线后优化 |
| 7 条 `NOT_COMPARABLE` | 保留 | 情绪次日一致性无法判定 | 保持 PARTIAL，不写 0 |

## 3. 行为变化（用户需知）

1. **手动同步停用**：自选页“同步”按钮、个股页首次自动补历史会收到 409；
   行情只由每日后台采集更新。首次打开的新票可能暂时没有本地 K 线。
2. **板块成分股**：GET 不再实时现取；当日未采集时显示最近一天名单 + 本地行情，
   并在页面说明。
3. **公告/研报/财务摘要**：仍会请求上游并返回数据，但普通请求不再写缓存，
   下次打开可能再次请求（配额/速度的代价）。
4. **真实竞价采集**：目标版本新增 `AUCTION_COLLECT_ENABLED=false`，部署后
   09:15–09:26 竞价任务默认不再注册；若仍需采集，需要显式开启并验收数据。

## 4. 生产环境现状提醒

- 当前生产仍在旧版本运行，**真实竞价采集仍在执行**（`auction_snapshot` 139,485 行）；
- 部署新版后该任务会停止（默认关闭），属于预期行为；
- TLS 证书 2026-12-27 到期，`certbot.timer` 已启用，上线前建议 `certbot renew --dry-run`。

## 5. 验收口径

```text
production_migration = NOT_EXECUTED
production_deploy = NOT_EXECUTED
production_core_acceptance = NOT_VERIFIED (needs real account)
code_and_staging_verification = PASS
```
