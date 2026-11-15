# S6.11 剩余风险登记

- 时间：2026-11-15
- 原则：`DEFERRED` / `NOT_RUN` / `NOT_VERIFIED` 不等于通过；本表不构成风险豁免。
- 责任人未指定的一律 `PENDING`。

## 1. 上线阻断项（必须解决或由用户书面接受）

| ID | 风险 | 证据 | 影响 | 严重度 | 缓解 | 允许上线 | 期限 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R-01 | 生产版本不可识别、无回滚基线 | 无 `.git`；`main.py` blob 不匹配任何历史提交；无发布包/SHA256/目录快照 | 出事无法回滚到已知状态 | 高 | 立即创建 `/opt/fupan-prev-*` 快照；补发布包与 SHA256 | **否** | 上线前 |
| R-02 | 生产单库可写，普通用户可触发研究表写入 | `watchlist/sync`、`stock/{code}/sync`、`sector/{code}/members` 均用 `get_db` 写 `stock_daily`/`sector_member`/`ifind_usage` | 用户请求可污染研究数据；无库级隔离 | 高 | 方案 A：`RESEARCH_WRITE_DISABLED` 守卫（见 `database-write-boundary.md` §9） | **否** | 上线前 |
| R-03 | Schema 漂移：`rps_daily` 缺 4 列 | 生产 49 表；缺 `ret20/ret60/rps20/rps60` | 目标版本 `schema_mode=verify` 启动失败 | 高 | 发布前执行 `migrate_rps_columns.py --apply`（可空、幂等、可回滚） | **否** | 上线前 |
| R-04 | 备份与主库同盘，无异地 | 备份日志原文“未配置异地同步”；5 份约 8.9 GB | 磁盘故障会同时丢失主库与备份 | 高 | 配置 `BACKUP_SYNC_DIR` 并验证同步成功 | **否** | 上线前 |
| R-05 | 无基础设施监控与告警渠道 | 无 exporter/Prometheus/Zabbix；无自定义 cron；仅 20:00 业务飞书告警 | 5xx/资源/延迟故障只能靠用户发现 | 高 | 最低监控集 + 指定渠道 + 实测送达（`monitoring-alert-readiness.md`） | **否** | 上线前 |
| R-06 | 生产出站无主机级限制，应用层 guard 未部署 | `OUTPUT ACCEPT`；`external_guard.py`/`egress_policy.py`/`egress-policy.json` 不存在 | 若代码被篡改可任意出站；无第二道网 | 高 | 部署目标版本 guard + 主机 nftables 策略；或书面接受 | **否** | 上线前 / 30 天 |
| R-07 | 生产真实竞价采集仍在运行 | `auction_snapshot` 139,485 行；调度日志 09:15–09:31；生产无 `AUCTION_COLLECT_ENABLED` | 数据未验收却持续采集；与 S6.10 目标行为不一致 | 中 | 目标版本默认关闭；上线时确认是否接受继续采集 | 可接受（需明示） | 上线时 |
| R-08 | 证书 2026-12-27 到期 | `openssl x509` notAfter | 自动续期失败会全站不可用 | 中 | `certbot.timer` 已 active；上线前 `certbot renew --dry-run` | 可接受（需验证） | 上线前 |
| R-09 | 数据质量 partial/empty 未告警 | 最近 8 天 `lhb partial ×5`、`members empty ×10`、`etf skipped ×10` | 页面数据静默缺失 | 中 | 纳入监控规则（数据日期/任务失败） | 可接受（需监控） | 上线前 |
| R-10 | readiness 端点不可用 | 生产无 `/api/ready`（401）；目标版本恒 503 | 发布验收与监控缺一致判据 | 中 | 按 `health-check-review.md` 拆分 live/ready/strict | **否** | 上线前 |
| R-11 | 责任人全部未指定 | 见 `release-responsibility.md` | 故障无人决策 | 高 | 用户指定 | **否** | 上线前 |
| R-12 | 生产环境标记为默认 development | 无 `FUPAN_ENV`；`WARMUP_PATTERN_CACHE=true` | 生产专用配置校验不生效 | 低 | 部署时设 `FUPAN_ENV=production` 并评估预热 | 可接受 | 上线时 |

## 2. 延续的已知风险（S6.10 登记，状态未变）

| ID | 风险 | 状态 | 说明 |
| --- | --- | --- | --- |
| R-13 | G3/G4 OS Egress / N01–N24 | **NOT_VERIFIED / NOT_RUN** | 本轮实测主机出站开放，未做绕过测试 |
| R-14 | 24h Soak | **NOT_RUN** | 只有 S6.9 短时 smoke |
| R-15 | 12 项长期故障注入 | 未完成 | 仅完成 5 项 |
| R-16 | `PERF_SQL_BUDGET_NOT_MET` 53 > 25 | 保留 | 未改预算/计数器/语义 |
| R-17 | 7 条 `NOT_COMPARABLE` | 保留 | 页面保持 PARTIAL，不写 0 |
| R-18 | Ubuntu VM / Guest Bootstrap | DEFERRED | `s6-egress-vm` 仍 Running，不删除 |

## 3. 本轮新增观察（不构成独立阻断，但需跟踪）

| 项 | 说明 |
| --- | --- |
| nginx 最近日志最大 rt 19.1s | 样本仅 306 请求，可能是个别重接口；上线后需 P95 监控 |
| `watchlist` 表 0 行 | 生产 5 个用户但无自选；确认是否符合预期（无数据丢失证据） |
| `lhb`/`lhb_institution` 连续 partial | 与 15:05 采集时刻的已知取舍一致，但未告警 |
| 生产 `user_session` 60 行 | 正常会话量级，无需处理 |

## 4. 延期事项（本轮不处理）

1. Ubuntu VM 安装与 Guest Bootstrap；
2. N01–N24 / OS Egress 完整验证；
3. 24h Soak；
4. 完整 12 项故障注入；
5. 非关键 SQL 优化；
6. 非关键架构重构。

## 5. 禁止

- 不得把本登记当作风险豁免；
- 不得删除 N01–N24 定义或历史证据；
- 不得为通过而修改阈值/预算/保护资产；
- 不得在未授权情况下部署、迁移生产库、开启灰度或合并 master。
