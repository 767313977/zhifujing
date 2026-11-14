# S6-C1 七条 NOT_COMPARABLE 登记

来源：`reports/s5_quality/contract_matrix.json`（`quality-consistency-matrix-v4`，
2026-11-14T23:59:59，真实库 `b1_research.db` 只读对账，`exit_code=2`）。
状态：**7 条全部保留**，未改成 `explained`，未从分母删除，未改动原始库数据。

| # | 观察日 | 检查项 | reason_code | 关键证据 |
| --- | --- | --- | --- | --- |
| 1 | 2026-09-14 | `sentiment.d1_coverage vs overview.stock_daily(D+1)` | `D1_DAILY_ROW_MISSING_WITHOUT_TRADABILITY_EVIDENCE` | D+1=2026-09-15；daily=VERIFIED_SOURCE；sentiment=PARTIAL；sample=44；valid=43；missing=1；缺行代码 `603400`；缺涨幅代码为空 |
| 2 | 2026-09-21 | `sentiment.d1_coverage vs overview.stock_daily(D+1)` | `D1_DAILY_ROW_MISSING_WITHOUT_TRADABILITY_EVIDENCE` | D+1=2026-09-22；sample=82；valid=81；missing=1；缺行代码 `002860` |
| 3 | 2026-09-24 | `sentiment.limit_up_again vs overview.limit_pool(D+1)` | `OUTCOME_LIMIT_POOL_DEPENDENCY_UNRESOLVED` | unverifiable=39；D+1 涨停池 `NOT_COLLECTED`、trusted=false；`limit_up_again=None`（不写 0） |
| 4 | 2026-09-29 | `sentiment.sample_pool(D up) vs overview.limit_pool(D)` | `SENTIMENT_SAMPLE_POOL_DEPENDENCY_UNRESOLVED` | `UP_POOL_MISSING`；sample=0；`overview.pool_up=NOT_COLLECTED` trusted=false |
| 5 | 2026-09-29 | `sentiment.metric(D) vs overview.market_sentiment(D)` | `SENTIMENT_METRIC_DEPENDENCY_UNRESOLVED` | `UP_POOL_MISSING` + `METRIC_MISSING`；`overview.market_sentiment=NOT_COLLECTED` |
| 6 | 2026-09-30 | `sentiment.sample_pool(D up) vs overview.limit_pool(D)` | `SENTIMENT_SAMPLE_POOL_DEPENDENCY_UNRESOLVED` | 同 #4：`UP_POOL_MISSING`；sample=0 |
| 7 | 2026-09-30 | `sentiment.metric(D) vs overview.market_sentiment(D)` | `SENTIMENT_METRIC_DEPENDENCY_UNRESOLVED` | 同 #5：`UP_POOL_MISSING` + `METRIC_MISSING` |

## 1. 判定原则（为什么不能解释成「停牌」）

#1 / #2 缺的是**下一交易日的日线行**。缺少交易资格证据时无法区分：
停牌、退市整理、数据源缺行、代码映射错误。任务书明确禁止把「缺行」推断成停牌，
因此只能 `NOT_COMPARABLE`。

#3–#7 缺的是**上游依赖**（涨停池 / 情绪指标），不是被比较对象本身。
把「依赖未采集」当成「结果为 0」会制造虚假的完整度，因此全部保留
`NOT_COMPARABLE`，并且所有计数保持 `None`（不是 0）。

## 2. 处理约束

- 不改变原始真实库数据；
- 不把 7 条转成 `explained`；
- 不从 `total_checks` 分母静默删除；
- 只有拿到**新的可核验证据**（例如交易所停牌公告、数据源补采成功记录）才允许
  另开提交更新单条判定；
- 真实库只读对账保留 `exit 2` 的 fail-closed 语义（见
  `reports/s6/data-quality/quality-state-contract.md` §4）。

## 3. 影响面

- 这些记录只影响「情绪次日」相关的一致性检查，不参与 C0 / Top20 / 交易信号；
- 灰度期间相关页面必须显示 `PARTIAL` / `NOT_COLLECTED` / `NOT_COMPARABLE` 语义，
  不得显示成 0 或「已验证」。
