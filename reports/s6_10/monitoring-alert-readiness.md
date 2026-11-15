# S6.10 P0：监控与应急预案就绪度

- 时间：2026-11-15
- 依据：`backend/app/readiness.py`、`api/auth.py`（health/ready）、`api/admin.py::/status`、
  `jobs/alerts.py`、`jobs/backup.py`、`deploy/setup_nginx.sh`、S6.9 短时 smoke 指标
- 生产监控系统（Prometheus / 云监控 / 告警通道）**未接入**：责任人、渠道均 `PENDING`

## 1. 服务监控

| 指标 | 现有能力 | 状态 |
| --- | --- | --- |
| 服务存活 | `GET /api/health`（liveness，端口起来即 200） | 可接入 |
| readiness | `GET /api/ready` | ⚠️ **恒 503**（`EGRESS_OS_POLICY_VERIFIED=NOT_VERIFIED` 刻意 fail-closed），**不能作健康判据** |
| HTTP 5xx | 无指标端点；只能看 nginx 日志 / journal | **缺口** |
| 请求延迟 | nginx `fupan_main` 含 `rt=` / `urt=` | 可采集（需日志系统） |
| CPU / 内存 | 无 exporter；`systemctl show -p MemoryCurrent` 可查 | **缺口** |
| 磁盘空间 | 无主动检查；仅 `df -h` 人工 | **缺口** |
| 数据库连接 | SQLite 单文件，无连接池指标；`integrity_check` 靠人工 | **缺口** |
| Worker / 队列 | 无队列；单进程 APScheduler（不可多 worker） | 不适用（需保证单 worker） |
| 调度任务状态 | `GET /api/admin/status` 返回 `next_run_time` / `last_run` / `last_result`（管理员） | 可用（需登录） |
| 采集日志 | `collect_log` 表；数据质量页 | 可用 |

结论：**只有 liveness + 调度状态 + 日志**可用；5xx/延迟/CPU/内存/磁盘没有主动告警，
属于上线前必须补的监控缺口（可先用云监控 + 日志关键字告警兜底）。

## 2. 数据监控

| 指标 | 现有能力 | 状态 |
| --- | --- | --- |
| 最新交易日数据 | `/api/market/dates`、质量页 `as_of` | 可用（需人工/脚本巡检） |
| 采集成功率 | `collect_log`（ok/failed/partial/skipped） | 可用 |
| 数据缺失 | 质量页 6 项 + `PARTIAL/NOT_COMPARABLE` 标记 | 部分可用 |
| 数据更新时间 | `collect_log.created_at`、`trade_date` | 可用 |
| 数据完整性 | `PRAGMA integrity_check`（备份脚本/人工） | 无定时检查 |
| 任务失败 | 20:00 告警检查 | 可用（仅交易日 20:00） |
| 备份失败 | 20:00 告警包含 `db_backup` 状态 | 部分可用（延迟到次日） |

### 2.1 现有告警逻辑（`jobs/alerts.py`）

20:00（`alert_hour`）检查并推一条飞书：

1. 简报未推送成功（`push_brief` 无 ok）；
2. 尾部链 `partial` / 未完成（`daily_tail`）；
3. 基础采集未完成（`collect_daily.RUN_ALWAYS_TASKS`）；
4. 配额进入让路档；
5. 最近一次备份不是 ok / 未配置异地同步。

只推一次（`collect_log(task=alert)` 去重）；非交易日跳过；未配置飞书通道时跳过。

## 3. 应急机制

### 3.1 服务异常

1. `systemctl status fupan`、`journalctl -u fupan -n 200`；
2. `curl /api/health`；若 502/超时，查 nginx 与端口；
3. 重启：`sudo systemctl restart fupan`（`Restart=always` 已兜底）；
4. 连续重启失败 → 按回滚流程切回上一版（`deployment-rollback-readiness.md` §6）。

### 3.2 数据采集失败

1. 看 `collect_log` 当天 `failed/partial` 任务与 `journalctl` 堆栈；
2. 确认是否配额让路（`/api/admin/status` 或用量页）；
3. **没有手动采集接口**；强制重跑只能 `systemctl restart fupan`（启动补采只补缺失步骤）；
4. 数据源故障时接受当天缺失，不手工改库、不伪造 0。

### 3.3 数据库异常

1. `PRAGMA integrity_check`；
2. 立即停服务，保留现场（`cp -p` 当前库）；
3. 按 `backup-restore-readiness.md` §3 恢复；
4. 恢复后核对关键表行数与业务摘要，再启动。

### 3.4 磁盘不足

1. `df -h`、`du -sh backend/data/*`；
2. 清理旧前端产物：`python scripts/clean_dist.py --dist frontend/dist`；
3. 备份保留 5 份（约 7.5 GB），必要时先迁走旧备份再删；
4. 不得为腾空间删除唯一有效备份或 WAL 正在使用的库。

### 3.5 备份失败

1. `journalctl -u fupan | grep db_backup`；
2. 检查磁盘空间与目录权限；
3. 手工触发一次备份（在 Python 环境调用 `app.jobs.backup.run()`）并核对 integrity；
4. 连续失败 → 按数据安全事件上报。

### 3.6 发布回滚

见 `deployment-rollback-readiness.md`：停服务 → 切回上一版目录 → 重启 → health/登录；
**不自动恢复数据库**。

### 3.7 安全事件

1. 立即停服务 / 断外网入口（nginx `return 503` 或防火墙）；
2. 保留 `journalctl`、nginx 日志、`collect_log`；
3. 检查 `external_guard` 拦截日志（`EGRESS_*`、`LOOPBACK_PORT_DENIED`）；
4. 轮换 `IFIND_AUTH_TOKEN`、飞书 webhook、`OPENAI_API_KEY`（如泄露）；
5. 不在事件中用“重装/删库”掩盖证据。

## 4. 责任人与渠道（必须由用户指定）

| 角色 | 值 |
| --- | --- |
| 上线负责人 | **PENDING** |
| 技术值班负责人 | **PENDING** |
| 告警渠道（短信/电话/IM 群） | **PENDING**（现有飞书 webhook 仅用于业务简报/收盘告警） |
| 回滚决策人 | **PENDING** |
| 回滚执行人 | **PENDING** |
| 观察窗口 | 建议 ≥48h（未执行） |

不编造人员与渠道。

## 5. 上线前最小补齐清单

1. 云监控或黑盒探测：`/api/health` 1 分钟一次，连续 3 次失败告警；
2. nginx 5xx 比例 + P95 延迟告警（基于 `rt/urt` 日志）；
3. 磁盘使用率 > 85% 告警；
4. 备份任务每日结果检查（独立于 20:00 汇总告警）；
5. 采集任务当天 `failed` 即告警（不等 20:00）；
6. 明确 `PENDING` 的责任人与渠道。

## 6. 结论

```text
liveness_endpoint = PASS
readiness_endpoint_usable_as_health = NO (always 503 by design)
http_5xx_latency_alert = MISSING
resource_alert = MISSING
scheduler_status_api = AVAILABLE
collection_alert_20h = PASS (trade days only)
backup_failure_alert = PARTIAL
owners_and_channels = PENDING
blocking_for_go_live = YES
```
