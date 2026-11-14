# S3 性能与对账（本地只读）

- 机器：本机 Windows，Python 3.13.13
- 数据：`backend/data/b1_research.db`（`mode=ro`，结束 2026-09-24）
- 统计方式：SQLAlchemy `before_cursor_execute` 计数 + `perf_counter`；无并发。
- 不宣称线上 SLA。

## A. 题材交叉（`/api/sectors/theme-search`）

| 案例 | 命中（已观测） | SQL | 耗时 |
| --- | --- | --- | --- |
| 2 板块 AND（芯片×算力） | 362 | 7 | 165.0 ms |
| 3 板块 AND（+人工智能） | 221 | 7 | 72.5 ms |
| 10 板块 OR | 4,811 | 7 | 118.0 ms |

覆盖（同 taxonomy、去重键 `(taxonomy, sector_code)`）：研究库 259/270，生产副本 19/270；
`has_completion_proof=false`，结果一律 `PARTIAL`，不标 `VERIFIED`。

## B. 情绪次日 · 断板分组批量优化

| 窗口 | S2 旧实现 SQL | S3 新实现 SQL | S3 耗时 | 有效观测 |
| --- | --- | --- | --- | --- |
| 20 日 | 185 | 33 | 186.5 ms | 901 |
| 60 日 | 545 | 82 | 521.1 ms | 3,143 |
| 120 日 | 1,085 | 153 | 1,154.9 ms | 6,983 |
| 250 日 | 2,255 | **300** | **2,500.9 ms** | 12,874 |

- 250 日 SQL 从 2,255 → 300（-87%），达到“≤300 SQL/250 日”目标；
- 6 个真实日断板代码逐日与逐日加载完全一致（74/33/55/76/46/36，`equal=true`）；
- 250 日峰值内存：**47.84 MB**（`tracemalloc`，包含结果对象；开启 tracing 时耗时会被放大，上表耗时为未开 tracing）。
- 首板 / 全部涨停 250 日仍约 255 SQL（与 S3 前一致），未被批量层影响。

## C. 个股研究依据（`/api/stock/{code}/research-context`）

| 样例 | SQL | 耗时 | 证据条数 |
| --- | --- | --- | --- |
| 002119 @ 2026-09-24 | 14 | 111.0 ms | 6 |

只读查询：当日池、池日志、全市场池行数、日线、情绪、题材、涨停天梯、交易日历、全局 data_as_of、
D+1 日线/池。无外部请求、无写入。

## D. 浏览器

Chrome 157.0.8103.79 headless，本地临时库 + 本地服务，16 项检查全部 PASS，控制台 0 error；
截图与 SHA256 见 `reports/s3_browser/browser_results.json`。
