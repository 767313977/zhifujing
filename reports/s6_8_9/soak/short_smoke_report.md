# S6.9-A 短时稳定性 Smoke

- 时间：2026-11-15T15:34:52+08:00
- 环境：隔离拆分 Staging（`127.0.0.1:8021`），Research 只读 + User 可写；
  外部取数/调度/真实采集关闭；独立临时库
- 时长：3.0 分钟

## 负载

| 指标 | 值 |
| --- | --- |
| 总请求 | 5918 |
| 研究读 | 3228 |
| 用户写成功 / 失败 | 1614 / 0 |
| 研究写拒绝（预期 503） | 1076 |
| 非预期 5xx | 0 |
| 传输错误 | 0 |
| P50 / P95 / P99 | 6.66 / 29.78 / 36.61 ms |
| `/api/ready` 503 | 538（OS Egress 门禁未验证，属预期） |

## 资源（每 2 秒采样，110 次）

| 项 | min / max / last |
| --- | --- |
| RSS | {'min': 4.12, 'max': 241.74, 'last': 211.05} |
| Private | {'min': 0.77, 'max': 636.5, 'last': 605.22} |
| Handles | {'min': 55, 'max': 210, 'last': 208} |
| Threads | {'min': 1, 'max': 23, 'last': 22} |
| CPU 秒（首末进程样本） | 1.953 |
| C: 剩余（首/末） | 225.06 → 223.71 GB |
| D: 剩余（首/末） | 470.52 → 470.52 GB |
| server.log（首/末） | 4673 → 4673 bytes |

连接池：SQLite 无受限连接池指标，记 `N/A`；worker 数：1；队列：无独立队列。

## 数据库与登录

- Research DB integrity：`ok`；User DB：`ok`
- 部署后核心流程（登录/刷新/深链/退出）：`True`
- 重启后核心流程：`True`

## 故障注入（5/5 PASS）

| 故障 | 结果 |
| --- | --- |
| SIGTERM | PASS（SIGTERM） |
| SIGKILL_RESTART | PASS（SIGKILL_RESTART） |
| USER_DB_READONLY_READINESS | PASS（USER_DB_NOT_WRITABLE） |
| RESEARCH_DB_MISSING_STARTUP_FAIL_CLOSED | PASS（RESEARCH_DB_MISSING_STARTUP_FAIL_CLOSED） |
| USER_DB_LOCK_CONTENTION | PASS（USER_DB_LOCK_CONTENTION） |

## 阈值

仓库未定义 5xx/延迟/内存/磁盘的正式 SLO，本轮按任务书记 `THRESHOLD_UNDEFINED`，
只报告测量值，不自造 PASS 线。

**状态：`S6_9_SHORT_SMOKE_PASS`（不满足/未执行 24h）**

> 短时 Smoke 通过不等于 24h Soak 通过。
