# S6-C5 历史数据缺口登记

状态：`HISTORICAL_DATA_GAP_UNDER_REVIEW`（保留，不关闭）。

## 1. 已核实的缺口（来自 v4 真实库只读对账）

| 日期 | 来源 | 缺失类型 | 受影响指标/页面 | 证据 |
| --- | --- | --- | --- | --- |
| 2026-09-15（D+1） | `stock_daily` | 单只股票日线行缺失（`603400`） | 情绪次日覆盖率 | `D1_DAILY_ROW_MISSING_WITHOUT_TRADABILITY_EVIDENCE` |
| 2026-09-22（D+1） | `stock_daily` | 单只股票日线行缺失（`002860`） | 情绪次日覆盖率 | 同上 |
| 2026-09-24（D+1） | 涨停池 | `NOT_COLLECTED` / trusted=false | `limit_up_again` | `OUTCOME_LIMIT_POOL_DEPENDENCY_UNRESOLVED` |
| 2026-09-29 | 涨停池（up） | `UP_POOL_MISSING` | `sample_pool`、`market_sentiment` | `SENTIMENT_SAMPLE_POOL_DEPENDENCY_UNRESOLVED`、`SENTIMENT_METRIC_DEPENDENCY_UNRESOLVED` |
| 2026-09-30 | 涨停池（up） | `UP_POOL_MISSING` | 同上 | 同上 |

## 2. 缺口分类（必须区分，不得合并成「无数据」）

| 类型 | 含义 | 处理 |
| --- | --- | --- |
| 未采集（NOT_COLLECTED） | 采集步骤没有成功记录 | 保留状态；不补 0 |
| 源失败（SOURCE_ERROR） | 请求/解析失败 | 保留 reason code；可重试，但不在对账时改判 |
| 历史不可回补 | 源站不提供历史或已过期 | 标记 `UNVERIFIABLE`，不伪造 |
| 覆盖未知 | 无法确认应有数量 | 标记 `SCOPE_INCOMPLETE` / 不进入分母 |
| 单行缺失（无交易资格证据） | 无法区分停牌/缺行/映射错误 | `NOT_COMPARABLE`（本轮 2 条） |

## 3. 受影响页面与指标

- 数据质量台：情绪/涨停池相关行；
- 盘后研究工作台：情绪次日；
- 涨停晋级漏斗：依赖涨停池的日期；
- 任何使用 `sentiment` 的统计：必须显示分母与缺口。

## 4. 严格 PIT 约束（未改变）

- 不使用未来数据回填历史观察日；
- `known_by_observation_time` / `historical_signal_eligible` 语义保持不变；
- 缺口日期不得用 0 填充，也不得把后续采集结果冒充为当时已知。

## 5. 结论

缺口仍在审查，本轮**没有**补造历史数据、没有修改质量判定、没有把
`NOT_COLLECTED` 改成 0。灰度期间相关页面必须继续显示缺口状态。
