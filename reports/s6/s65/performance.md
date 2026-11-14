# S6.5 性能回归

- 基线：`benchmark_s5.py`，10 板块，2026-09-24，`b1_research.db` 只读
- 输出：`reports/s6/s65/benchmark.json`

| 用例 | SQL | P50 | 峰值内存 |
| --- | --- | --- | --- |
| workbench 1 板块 | 53 | 124.06 ms | 2.4 MB |
| workbench 2 板块 | 53 | 103.09 ms | 3.3 MB |
| workbench 3 板块 | 53 | 107.28 ms | 3.7 MB |
| **workbench 10 板块** | **53** | **123.48 ms** | **5.6 MB** |
| quality overview（30 天） | 21 | 47.37 ms | 57 KB |
| quality timeline（250 天） | 7 | 32.30 ms | 3.9 MB |
| sentiment next day（250 天） | 255 | 365.86 ms | 13.5 MB |
| research context 单股 | 14 | 15.90 ms | 2.1 MB |

## 结论

- **`PERF_SQL_BUDGET_NOT_MET` 保留**（53 > 25），未修改预算、未删查询；
- 响应时间与上一轮同量级（工作台 123–124 ms），未因 S6.1/S6.2 改动退化；
- 冷/热缓存未单独区分；本轮为热缓存。
