# S6.12 上线方案汇总（等待授权）

- 时间：2026-11-15
- 状态：**代码与方案完成；生产迁移/部署/重启均未执行，等待用户明确授权。**

## 1. 代码修改清单

| 文件 | 修改 |
| --- | --- |
| `backend/app/runtime_context.py` | 新增请求级研究写入上下文 |
| `backend/app/main.py` | 新增 `research_write_boundary` 中间件（`/api/admin/*` 显式放行） |
| `backend/app/api/watchlist.py` | `POST /sync` → 409，不再调用采集器 |
| `backend/app/api/stock.py` | `POST /{code}/sync` → 409，不再调用采集器 |
| `backend/app/api/sector.py` | 成分股 GET 纯读，不再现取/落库 |
| `backend/app/services/stock_events.py` | 请求内跳过事件缓存写入 |
| `backend/app/services/financials.py` | 请求内跳过财务缓存写入 |
| `backend/app/services/usage.py` | 请求内跳过 iFinD 计量写入 |
| `frontend/src/pages/Watchlist.tsx` | 409/503 显示提示而非错误 |
| `tests/test_s6_12_write_boundary.py` | 新增 7 项负向测试 |
| `tests/test_stock_sync_guard.py` | 按新行为更新 |
| `scripts/s6_12_rps_migration_drill.py` | 新增迁移演练脚本 |
| `scripts/s6_12_staging_smoke.py` | 新增隔离 Staging 冒烟脚本 |

提交：`621e11a`、`a8b13a6`。

## 2. 测试结果

- 后端：**1758 passed, 4 skipped**；
- 新增边界测试：**7 passed**；
- 前端：lint 0 error、168 tests passed、build 成功；
- RPS 迁移演练：**PASS（17/17）**；
- 隔离 Staging 冒烟：**PASS**；
- Ruff：All checks passed。

## 3. 生产备份信息

| 项 | 值 |
| --- | --- |
| 最近备份 | `fupan.db-bak-20261113-231000-001069` |
| 大小 | 1,834,606,592 字节 |
| SHA256 | `c96b29b27b93912b455d9bda1b1e7c0d9a4cc4da8dd3b2564f733f20d2b6efd3` |
| integrity | ok（2,627,342 行 `stock_daily`，最新 2026-11-13） |
| 异地备份 | 未配置（用户同意暂缓） |
| 旧版完整副本 | `dist-deploy/rollback-prod-20261115.tar.gz`，SHA256 `6a377886…`，`main.py` 与生产一致 |

## 4. Schema 迁移结果

- 演练：备份 → 迁移 → 幂等 → 中断恢复 → 旧列兼容 → 新版本 verify，全部 PASS；
- **生产迁移未执行**；
- 方案与命令见 `02-rps-migration.md`。

## 5. 生产部署结果

- **未部署**；
- 目标包：`dist-deploy/fupan-2026-11-15.tar.gz`，SHA256 `447af533…`，304 文件，无 `.env`/数据库；
- 部署顺序：备份 → 迁移 → 部署 → 重启 → 验收（见 `04-release-plan-and-version.md`）。

## 6. 网站核心功能验收

| 项 | 结果 |
| --- | --- |
| 公网可访问 | PASS（`/`、`/login`、`/api/health` 均 200） |
| 登录/首页/个股/板块（真实账号） | **NOT_VERIFIED**（无生产账号凭据） |
| 最新交易日数据 | 生产库 2026-11-13（只读查询） |
| 采集任务 | 调度器运行中（S6.11 探测） |
| 数据库完整性 | ok |

## 7. 未解决问题

- 生产版本不可识别（已用完整文件副本作为回滚基线）；
- 生产登录验收待账号；
- 异地备份、监控告警、OS Egress、24h Soak 均按用户要求延期，未标 PASS；
- 手动同步停用后，新票首次打开可能暂无本地 K 线。

## 8. 回滚情况

- 未执行回滚；
- 回滚命令与数据库恢复步骤已写入 `03-backup-rollback.md`；
- 发布失败优先恢复旧代码，不删除新增列、不自动恢复数据库。

## 9. 请求授权

请确认后我再执行：

1. 生产 RPS 迁移；
2. 上传并解压目标包；
3. `sudo bash deploy/install.sh`（会重启 `fupan.service`）；
4. 核心功能验收（需要你提供可用的测试账号，或由你自行登录验收）。

在你明确授权前，不会对生产做任何写入。
