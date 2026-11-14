# S5 性能基准

- 生成日期：2026-11-14（Asia/Shanghai）
- 输入：`backend/data/b1_research.db`（`mode=ro&immutable=1`，真实历史只读库）
- 观察日：2026-09-24
- 机器：Windows 本机，Python venv，SQLite 页缓存预热后重复 3 次取中位数
- 原始数据：`reports/s5_performance/benchmark.json`

## 1. 结果

| 用例 | 参数 | SQL 条数 | 耗时中位数 | 峰值内存 | JSON 大小 |
| --- | --- | --- | --- | --- | --- |
| workbench | 1 板块 OR | 50 | 227.21 ms | 3.9 MB | 20.1 KB |
| workbench | 2 板块 OR | 50 | 172.53 ms | 4.9 MB | 20.6 KB |
| workbench | 3 板块 OR | 50 | 156.50 ms | 6.8 MB | 20.6 KB |
| workbench | 10 板块 OR | 50 | 352.06 ms | 11.3 MB | 21.4 KB |
| quality overview | 单日 / 30 日窗口 | 18 | 51.56 ms | 53 KB | 4.8 KB |
| quality timeline | limit_pool / 250 日 | 4 | 38.26 ms | 3.5 MB | 402 KB |
| sentiment next-day | 250 日 | 255 | 949.01 ms | 13.0 MB | 177 KB |
| research-context | 单股单日 | 14 | 26.33 ms | 2.2 MB | 9.1 KB |

## 2. 目标对照

任务书目标：**10 板块窗口 ≤25 SQL、<500ms**（本地只读库，非先验保证）。

- 耗时：**352 ms < 500 ms，达标**；
- SQL：**50 条 > 25 条，未达标**。

未造数、未删用例。瓶颈拆分（同一日期、3 板块实测）：

| 区块 | SQL 条数 | 说明 |
| --- | --- | --- |
| `sentiment_next_day` | 6 | 情绪 + D+1 窗口 |
| `limit_funnel` | 9 | 昨日池 / 今日池 / 日线 / 日志 / 交易日历 |
| `data_quality.overview` | 18 | 六个来源各自 `max(date)` + 明细 |
| `sector_resonance` | 15 | 成员快照 / 池 / 日线 / 情绪 / 日志 |
| 工作台自身 | 2 | 交易日历 + `max(stock_daily)` |

SQL 条数**与板块数量无关**（1/2/3/10 板块均为 50），因为聚合按日期与代码集合批量查询，不做逐板块/逐券查询。要把 50 降到 25，需要把四个服务的读模型合并成一次批量查询或引入只读快照层，属于跨模块重构，本批次不做，避免为达标而降低可审计性。

## 3. 说明

- 内存用 `tracemalloc` **单独一次**测量，避免把 profiler 开销混入耗时；
- `sentiment_next_day` 的 SQL 随天数增长（约 1 条/日），与股票只数无关；250 日是 255 条、949 ms；
- 以上为本地只读库实测，不是线上 SLA；
- 未测真实并发、冷启动磁盘 IO 与网络延迟。
