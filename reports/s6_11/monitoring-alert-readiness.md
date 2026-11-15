# S6.11 最低可用监控与告警就绪度

- 时间：2026-11-15
- 生产证据：`production-readonly-probe.md` §15/§16、`evidence/prod-probe-raw.txt`
- **未创建监控配置、未发送任何告警、未修改生产**
- 结论：**不满足上线最低要求（BLOCKED）**，且告警渠道 `PENDING`

## 1. 生产现状（OBSERVED）

| 项 | 实测 |
| --- | --- |
| 监控代理 | **无**（无 node_exporter / Prometheus / Zabbix / Grafana / netdata） |
| 系统 cron | 仅腾讯云 stargate，无自定义巡检 |
| 应用内告警 | 20:00 飞书收盘链路告警（简报/尾部/基础采集/配额/备份） |
| 日志轮转 | `fupan` 无 logrotate；nginx 有 |
| journal 占用 | 472.4 MB |
| 最近日志 5xx | 0 / 306 请求（样本很小，不能作为长期结论） |
| 公网探活 | `https://zhifujing.top/api/health` = 200 |
| readiness | 生产无该路由（401） |

## 2. 最低监控要求对照

### 2.1 服务

| 指标 | 现状 | 状态 |
| --- | --- | --- |
| HTTP 5xx | 只有 nginx 访问日志，无采集/阈值 | **MISSING** |
| 请求延迟 | nginx `rt=` / `urt=` 有记录，无采集/阈值 | **MISSING** |
| 服务存活 | `/api/health` 200；无拨测 | **PARTIAL** |
| readiness | 生产无端点；目标版本恒 503 | **MISSING** |
| CPU | 无采集 | **MISSING** |
| 内存 | `systemctl show` 可查，无采集 | **MISSING** |
| 磁盘空间 | `df` 可查，无采集/阈值 | **MISSING** |

### 2.2 数据

| 指标 | 现状 | 状态 |
| --- | --- | --- |
| 最新交易日数据 | 库内可查（最新 2026-11-13）；无自动巡检 | **PARTIAL** |
| 采集失败 | `collect_log` + 20:00 飞书告警 | **PARTIAL** |
| 数据缺失 | 质量页 + `partial/empty` 标记；无自动告警 | **PARTIAL** |
| 数据更新时间 | `collect_log.created_at` 可查 | **PARTIAL** |
| 数据库完整性 | 仅人工 `integrity_check` | **MISSING** |

### 2.3 任务

| 指标 | 现状 | 状态 |
| --- | --- | --- |
| 调度失败 | APScheduler 日志；无独立告警 | **PARTIAL** |
| Worker 失败 | 单进程 `Restart=always`；无重启计数告警 | **MISSING** |
| 备份失败 | 20:00 汇总告警（次日才提示，非交易日跳过） | **PARTIAL** |

### 2.4 已知非 ok 任务（最近 8 天）

```text
lhb partial ×5
lhb_institution partial ×5
members empty ×10
etf skipped ×10
sentiment partial ×1
```

这些目前不会触发告警（20:00 告警只看简报/尾部/基础采集/配额/备份），属于**静默数据质量下降**。

## 3. 告警渠道

| 项 | 状态 |
| --- | --- |
| 渠道 | **PENDING**（未指定短信/电话/IM/邮件） |
| 接收人 | **PENDING** |
| 测试送达 | **未执行**（无渠道，且不应向业务飞书群发送测试告警） |
| 现有飞书 webhook | 仅用于业务简报与 20:00 收盘告警，**不能**当作主机/5xx 告警渠道 |

按任务书要求：**没有实际接收渠道，不得宣称告警可用**。

## 4. 建议的最低方案（待用户确认渠道后实施）

### 4.1 采集

- 云监控「云拨测」或外部黑盒：`https://zhifujing.top/api/live`，30–60s；
- 主机指标：腾讯云监控自带（CPU/内存/磁盘）或 `node_exporter`；
- nginx：日志采集 `rt/urt` 与 `$status`，聚合 5xx 比例、P95；
- 应用：`/api/ready`（按 `health-check-review.md` 改造后）+ `/api/health?detail=1`（内网）。

### 4.2 告警规则（最低集）

| 规则 | 阈值 | 级别 |
| --- | --- | --- |
| 公网拨测失败 | 连续 3 次（约 3 分钟） | P1 |
| `/api/ready` != 200 | 连续 5 分钟 | P1 |
| 5xx 比例 | 5 分钟 > 1% 且请求数 > 20 | P1 |
| P95 延迟 | 5 分钟 > 2s | P2 |
| 磁盘使用率 | > 85% | P1 |
| 内存使用率 | > 90% 持续 10 分钟 | P2 |
| 采集任务 failed | 当天任一关键任务 failed | P1 |
| 数据日期滞后 | 交易日 18:00 后最新日期 < 当日 | P1 |
| 备份失败/缺失 | 交易日 23:30 后无 ok 记录 | P1 |
| DB integrity != ok | 每日巡检 | P1 |

### 4.3 送达测试（实施时必须做）

1. 人为触发一条测试告警；
2. 记录发送时间、渠道、接收人、回执；
3. 截图/消息 ID 存入 `reports/s6_11/`；
4. 未收到 → 视为未完成，不得写 PASS。

## 5. 结论

```text
infra_monitoring = MISSING
service_5xx_latency_alert = MISSING
resource_alert = MISSING
data_quality_alert = PARTIAL (only 20:00 summary)
backup_failure_alert = PARTIAL (next-day)
alert_channel = PENDING
alert_delivery_tested = NO
monitoring_alert_readiness = BLOCKED
```
