# S6.9 24 小时 Staging Soak 报告

- 状态：**`SOAK_24H_NOT_RUN`**
- 连续时长：0 小时（未启动正式计时）
- 短时 Smoke：**`S6_9_SHORT_SMOKE_PASS`**（3 分钟，5918 请求，0 非预期 5xx，5/5 故障注入 PASS）
- 证据：`short_smoke_report.md`、`short_smoke_metrics.json`、`short_smoke_timeline.jsonl`、
  `short_smoke_fault_matrix.md`、`short_smoke_resources.json`、`resource_samples.jsonl`

## 1. 为什么 24h 未执行

| 条件 | 状态 |
| --- | --- |
| Release Critical | 本轮补齐中（应用层 SSRF、备份恢复、回归待收尾） |
| OLD/NEW 真回滚 | PASS |
| G7 | PASS |
| G0 | before PASS；final 待收尾 |
| OS Egress N01–N24 | **DEFERRED / NOT_VERIFIED** |
| 24h 连续环境 | 当前开发机无法保证不中断；任务书允许短时 Smoke 替代，但不得称 24h 通过 |
| 监控/告警/停止机制 | 未建立 24h 告警通道 |
| 既有 SLO/阈值 | **THRESHOLD_UNDEFINED** |

因此按平衡方案执行短时 Smoke，24h 保留 `NOT_RUN`，不拼接、不伪报。

## 2. 短时 Smoke 实测（摘要）

- 总请求 5918；研究读 3228；用户写 1614/1614；研究写拒绝 1076；
- 非预期 5xx **0**；传输错误 **0**；P50/P95/P99 = 6.66 / 29.78 / 36.61 ms；
- `/api/ready` 503 属预期（OS Egress 未验证）；
- 部署后与重启后核心登录流程均 PASS；
- 故障注入：SIGTERM、SIGKILL 重启、User DB 只读、Research DB 缺失 fail closed、
  锁争用有界失败，5/5 PASS；
- Research/User DB integrity = `ok`。

## 3. 未覆盖（24h 专属）

- 连续 24h 的内存/句柄/线程/锁/磁盘趋势；
- 受控磁盘降容与 VM 重启后 Egress 持久性；
- 多 worker/队列（当前单 worker、无独立队列）；
- 生产级监控告警与自动停止。

## 4. 解除条件

1. Release Critical 全部通过；
2. 生产/隔离环境具备 24h 不中断与监控告警；
3. 定义或引用正式 SLO/错误率阈值（否则继续 `THRESHOLD_UNDEFINED`）；
4. 按任务书在隔离 Staging 启动计时，逐分钟采集；
5. 故障注入有快照/回滚保护；触发中止条件即重新计时。

> 短时 Smoke 通过不等于 24h Soak 通过。
