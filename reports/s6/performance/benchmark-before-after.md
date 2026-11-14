# S6-D1 性能基准：before / after

## 1. 结论

**`PERF_SQL_BUDGET_NOT_MET` 保留。** 10 板块工作台稳定在 **53 条 SQL**，
预算目标 25 未达到；本轮**没有**修改预算、没有为了凑数删查询、没有改返回语义。

## 2. 基准对比（同机、同库、同参数）

- 库：`backend/data/b1_research.db`（`mode=ro&immutable=1`）
- 日期：2026-09-24；板块：801001,801004,801012,801016,801019,801020,801027,801031,801033,801035
- 代码：`release/s6-canary-hardening` @ `104dfdf`（before 与 after 同一份代码，未做优化）

| 用例 | before SQL | before P50 | after SQL | after P50 | peak 内存 |
| --- | --- | --- | --- | --- | --- |
| workbench 1 板块 | 53 | 128.81 ms | 53 | 131.23 ms | 2.4 MB |
| workbench 2 板块 | 53 | 113.98 ms | 53 | 105.97 ms | 3.3 MB |
| workbench 3 板块 | 53 | 113.97 ms | 53 | 121.07 ms | 3.7 MB |
| **workbench 10 板块** | **53** | **165.28 ms** | **53** | **135.94 ms** | **5.6 MB** |
| quality overview（30 天） | 21 | 47.18 ms | 21 | 52.49 ms | 57 KB |
| quality timeline limit_pool（250） | 7 | 29.42 ms | 7 | 30.83 ms | 3.9 MB |
| sentiment next day（250） | 255 | 1881.35 ms | 255 | 464.55 ms | 13.5 MB |
| research context 单股 | 14 | 12.96 ms | 14 | 18.62 ms | 2.1 MB |

说明：SQL 数量完全相同；耗时波动属于同机噪声（sentiment 250 天差异更大，
主要是页面缓存热度，不代表代码优化）。**不能据此宣称性能提升。**

## 3. SQL waterfall（10 板块）

`reports/s6/performance/sql-waterfall.json`：

- 总 SQL：53；SQL 自身总耗时 38.8 ms；墙钟 162.5 ms（其余为 ORM/Python 组装）；
- 重复语句（按规范化语句聚合）：

| 次数 | 总耗时 | 语句摘要 |
| --- | --- | --- |
| 5 | 1.79 ms | `SELECT limit_pool...` |
| 3 | 0.62 ms | `SELECT collect_log...` |
| 3 | 0.58 ms | `SELECT trade_calendar...` |
| 4 | 0.25 ms | `SELECT max(stock_daily.trade_date)...` |
| 2 | 0.74 ms | `SELECT collect_log.trade_date ...` |

- 结论：重复查询合计约 **4 ms**，即使全部合并也远不足以把 53 降到 25；
  真正的成本在**固定数量的独立查询**（质量/池/情绪/板块各一组）。

## 4. 为什么本轮不做优化

1. 25 条预算需要跨服务重构（把多组独立查询合并成批量查询），不是低风险改动；
2. 任务书要求「结果逐字段等价」，而工作台输出 20 KB JSON、字段众多，
   在没有完整黄金样本对比设施时改动风险高；
3. 当前 P50 ≈ 136–165 ms，未观察到超时或锁等待；性能风险应在
   `performance-risk-register.md` 中披露，而不是用改预算掩盖。

## 5. 建议的后续优化（未实施）

1. 把 `limit_pool` 的 5 次查询按日期区间合并为 1 次；
2. `trade_calendar` / `collect_log` / `max(stock_daily)` 做请求级 memo；
3. 为 `post_market_workbench.build` 建立字段级黄金样本，再做批量查询改造；
4. 目标：先降到 ≤35，再评估 25 是否可达（预算值本身需用户批准变更）。
