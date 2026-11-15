# S6.8 F2：7 条 NOT_COMPARABLE 复核

- 原始登记：`reports/s6/data-quality/not-comparable-register.md`
- 上游口径：`quality-consistency-matrix-v4`（`reports/s6/data-quality/contract_matrix_v4.json`，
  `quality_version` / `matrix_version` 未变，`exit_code=2` 的 fail-closed 语义保留）
- 本轮复核：只读对照台账与 v2/v3 证据；未查询生产库、未补采、未改分类
- 结论：**7 条全部原样保留**，无新增权威证据，`NOT_COMPARABLE` 数量不变

## 逐条核对

| # | 观察日 | 检查项 | reason_code | 复核结论 |
| --- | --- | --- | --- | --- |
| 1 | 2026-09-14 | sentiment.d1_coverage vs overview.stock_daily(D+1) | `D1_DAILY_ROW_MISSING_WITHOUT_TRADABILITY_EVIDENCE` | 保留；缺行 `603400`，仍无交易资格证据 |
| 2 | 2026-09-21 | 同上 | 同上 | 保留；缺行 `002860` |
| 3 | 2026-09-24 | sentiment.limit_up_again vs overview.limit_pool(D+1) | `OUTCOME_LIMIT_POOL_DEPENDENCY_UNRESOLVED` | 保留；D+1 涨停池 `NOT_COLLECTED`，`limit_up_again` 仍为 `None` |
| 4 | 2026-09-29 | sentiment.sample_pool(D up) vs overview.limit_pool(D) | `SENTIMENT_SAMPLE_POOL_DEPENDENCY_UNRESOLVED` | 保留；`UP_POOL_MISSING`，sample=0 |
| 5 | 2026-09-29 | sentiment.metric(D) vs overview.market_sentiment(D) | `SENTIMENT_METRIC_DEPENDENCY_UNRESOLVED` | 保留；`UP_POOL_MISSING` + `METRIC_MISSING` |
| 6 | 2026-09-30 | sentiment.sample_pool(D up) vs overview.limit_pool(D) | `SENTIMENT_SAMPLE_POOL_DEPENDENCY_UNRESOLVED` | 保留；同上 |
| 7 | 2026-09-30 | sentiment.metric(D) vs overview.market_sentiment(D) | `SENTIMENT_METRIC_DEPENDENCY_UNRESOLVED` | 保留；同上 |

## 复核要点

1. **缺行 ≠ 停牌**：#1/#2 没有交易所停牌公告或数据源补采成功记录，不能推断原因；
2. **依赖未采集 ≠ 0**：#3–#7 的上游池/指标未采集，计数保持 `None`；
3. **不改变分母**：`total_checks` 未删除任何一条；
4. **不改写历史**：v2/v3 证据与 `exit 2` 语义均保留；
5. **无新权威证据**：本轮未获得停牌公告、补采记录或新的对账样本。

## 保持的状态

```text
NOT_COMPARABLE = 7（不变）
THEME_REAL_DATA_ACCEPTANCE_PENDING
REAL_AUCTION_DATA_NOT_VERIFIED
HISTORICAL_DATA_GAP_UNDER_REVIEW
PERF_SQL_BUDGET_NOT_MET
```

灰度页面必须继续按 `PARTIAL` / `NOT_COLLECTED` / `NOT_COMPARABLE` 展示，
不得显示为 0 或“已验证”。
