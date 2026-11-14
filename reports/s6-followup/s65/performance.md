# S6.5：性能复核（SQL 预算与耗时）

- 命令：`python scripts/benchmark_s5.py --db backend/data/b1_research.db --date 2026-09-24 --sectors 801572,801366,803024,801085,801033 --out reports/s6-followup/s65/benchmark.json`
- 数据库：`b1_research.db`（只读 `mode=ro&immutable=1`）
- 证据：[benchmark.json](./benchmark.json)

## 结果

| 用例 | SQL 条数 | 中位耗时 | 峰值内存 | JSON 体积 |
| --- | --- | --- | --- | --- |
| workbench（1 板块） | 53 | 100.37 ms | 3.9 MB | 20.1 KB |
| workbench（2 板块） | 53 | 110.70 ms | 4.8 MB | 20.6 KB |
| workbench（3 板块） | 53 | 104.57 ms | 6.6 MB | 20.6 KB |
| workbench（5 板块） | 53 | 107.39 ms | 8.2 MB | 20.9 KB |
| quality overview（1 天） | 21 | 36.53 ms | 56 KB | 5.1 KB |
| quality timeline（limit_pool 250） | 7 | 22.91 ms | 3.4 MB | 426 KB |
| sentiment next-day（250） | 255 | 287.70 ms | 13.2 MB | 173 KB |
| stock research-context（单票） | 14 | 10.89 ms | 2.1 MB | 9.8 KB |

## 结论

- **workbench 仍是 53 条 SQL，预算 25 条未达成**：`PERF_SQL_BUDGET_NOT_MET`
  保持原口径，不得改预算、不得宣称已关闭；
- 耗时较上一轮（中位 123–131 ms）略降，但 SQL 条数没有变化；
- 本轮未做 N+1 优化（避免在没有等价性测试的前提下改查询结构）。
