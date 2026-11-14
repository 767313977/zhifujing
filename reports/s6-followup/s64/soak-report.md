# S6.4：拆分形态 Staging 短窗 soak + 故障注入

> **不是 24 小时 soak。** 本机无法保持 24h 连续环境，按任务书如实标注
> `S6_STAGING_SOAK_PASSED = NOT_RUN`，不以短窗冒充。

- 执行命令：`python scripts/s7_soak_split.py --minutes 3 --faults`
- 证据：[soak-metrics.json](./soak-metrics.json)、[fault-injection.json](./fault-injection.json)、
  [staging-server.log](./staging-server.log)

## 1. 环境

| 项 | 值 |
| --- | --- |
| 形态 | `DB_SPLIT_ENABLED=true`（Research `mode=ro` + User 可写） |
| 外部取数 | `EXTERNAL_DATA_FETCH_ENABLED=false`（已锁定离线） |
| 调度器 / 预热 | 关闭 |
| 数据库 | 隔离临时目录下的 research.db（只读）/ user.db（可写），用完删除 |
| 服务 | 本机 uvicorn `127.0.0.1:8021`，单进程 |

启动日志确认：Schema 检查通过（Research 37 张研究表，不含用户表）、
`scheduler 已关闭`、`外部数据现取已被禁用`。

## 2. 负载结果（3 分钟）

| 指标 | 值 |
| --- | --- |
| 总请求 | 5,632 |
| 研究读 | 3,072（全部 200） |
| 用户写成功 | 1,536 / 1,536（0 失败） |
| 研究写拒绝（预期 503） | 1,024 / 1,024 |
| `/api/ready` 503 | 512（预期：OS Egress 门禁未验证） |
| 5xx（不含预期 503） | **0** |
| 传输错误 | **0** |
| P50 / P95 / P99 | 14.60 / 29.35 / 33.31 ms |
| User DB integrity | `ok` |
| Research DB integrity | `ok` |

端点覆盖：`/api/health`、`/api/ready`、`/api/market/overview`、
`/api/limit/promotion`、`/api/sentiment/next-day`、`/api/research/workbench`；
用户写：`POST/DELETE /api/watchlist`、`PUT /api/risk/pref`；
研究写拒绝：`POST /api/watchlist/sync`、`POST /api/stock/{code}/sync`。

## 3. 故障注入（全部 PASS）

| 故障 | 结果 | 证据 |
| --- | --- | --- |
| SIGTERM | 进程退出 | `exit_code=1`（uvicorn 被信号终止） |
| SIGKILL + 重启 | health 200，User DB `integrity=ok` | — |
| User DB 只读 | `/api/ready` 503，`DATABASE_READ_ONLY_VERIFIED=USER_DB_NOT_WRITABLE` | readiness 实时重算生效 |
| Research DB 缺失 | 新进程**启动即退出**（exit 3），端口不可用 | fail closed，不自动建空库 |
| User DB 写锁争用 | 请求 16.5s 后有界失败（500），未死锁 | 小于 20s 上限；错误可见 |

## 4. 与 24h soak 的差距（必须如实保留）

- 只有 3 分钟；无法覆盖交易日盘中/收盘后的长时行为；
- 未采集 CPU / RSS / 文件描述符 / SQLite busy 计数（未安装 psutil）；
- 没有真实 OS 层 Egress 计数器（无独立 VM）；
- 未覆盖「多 worker 同时启动」「磁盘满」「进程重启后策略持久性」。

因此：

```text
S6_STAGING_SOAK_PASSED = NOT_RUN
```

## 5. 并发说明

本轮 soak 为单线程顺序请求，不是并发矩阵；并发回归沿用上一轮
`reports/s6/performance/concurrency-regression.json`（60/60）与
`tests/test_p0_cache_concurrency.py` 等既有测试，本轮全量 pytest 通过。
