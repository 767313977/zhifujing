# S5-A PIT v3 时点协议

- 版本：`research-evidence-v3`
- 适用接口：`GET /api/stock/{code}/research-context`
- 时区：`Asia/Shanghai`（+08:00，无夏令时）
- 生成日期：2026-11-14

## 1. 三个时间概念

| 概念 | 字段 | 含义 |
| --- | --- | --- |
| 事件归属日 | `source_trade_date` | 行情/涨停/题材快照归属的交易日 |
| 本站取得时间 | `observed_at` / `fetched_at` | 本系统**写入/观察到**该证据的时间（naive，按 `models._now()` 约定为 Asia/Shanghai） |
| 观察截止时刻 | `cutoff_at` | 本轮唯一正式支持的 `cutoff=CLOSE` = 观察日 **15:00 Asia/Shanghai**（含 15:00 整） |

`source_published_at` 只有真实来源可独立核验时才填；当前所有来源都拿不到，一律 `null`，**不得**用 `trade_date 15:00` 伪造。

## 2. 判定规则（`evaluate_known_time`）

1. 来源完成性不成立（日志缺失/partial/failed/ok+0 异常）→ `UNKNOWN`（`SOURCE_NOT_READY`），时间再早也不算已知。
2. `observed_at` 缺失 → `UNKNOWN`（`OBSERVED_AT_MISSING`）。
3. naive 时间且不信任 Asia/Shanghai 约定 → `UNKNOWN`（`NAIVE_TIMESTAMP_NO_TRUSTED_TZ`）。
4. 最早可用时刻 `known_at = max(observed_at, published_at)`。
5. `known_at <= cutoff_at` → `CLOSE`，进 `AT_CUTOFF_KNOWN`。
6. 同日但晚于 cutoff → `POST_CLOSE_UNKNOWN`（`AFTER_CUTOFF_SAME_DAY`），进 `UNKNOWN_OR_LATER`。
7. 更晚日期 → `LATER`（`AFTER_CUTOFF_LATER_DAY`），进 `UNKNOWN_OR_LATER`。
8. D+1 结果 → `FORWARD`，只进 `FORWARD_OUTCOME`，永不回填 D 日。

## 3. 各来源口径

| 来源 | 时间证据 | 结论 |
| --- | --- | --- |
| `limit_pool` | `collect_log.created_at`（按池分别判断） | 15:00 之后 → `POST_CLOSE_UNKNOWN`；三池任一缺失/失败 → 该断言降级 |
| `sector_member` | `fetched_at` | 观察日之前 → `PRIOR_DAY`；同日 → `SAME_DAY_UNKNOWN`；次日 → `LATER` |
| `stock_concept` | 无可审计发布时间 | 永久 `POST_CLOSE_UNKNOWN` |
| `stock_daily` | 无首次入库时间 | `END_OF_DAY_FACT`：只作复盘，不进已知桶 |
| `market_sentiment` | 无首次入库时间 | `END_OF_DAY_FACT`：只作复盘，不进已知桶 |

## 4. 三桶与兼容字段

```text
AT_CUTOFF_KNOWN  ← CLOSE / PRIOR_DAY
UNKNOWN_OR_LATER ← SAME_DAY_UNKNOWN / LATER / POST_CLOSE_UNKNOWN / UNKNOWN / END_OF_DAY_FACT
FORWARD_OUTCOME  ← FORWARD
```

- 三桶互斥且并集守恒，由 `validate_time_bucket_invariants` 机器断言；
- `as_known_on_day` 只是 `at_cutoff_known` 的子集，**不含** LATER、同日未知、盘后未知、日终事实；
- `end_of_day_fact` 独立输出，并附 `END_OF_DAY_FACT_NOT_AT_CUTOFF` 原因码。

## 5. 哪些历史事实只能用于复盘

- `stock_daily` 的收盘价/涨跌幅：只能说明「D 日收盘后的客观结果」，不能证明 15:00 当时本站已知，不能当作可交易的盘中信号。
- `market_sentiment` 的涨停家数/最高连板：同上。
- 15:00 之后才写入的 `collect_log`（多数定时采集在收盘后）：其支持的池状态一律进 `UNKNOWN_OR_LATER`。
- D+1 行情/再涨停：只进 `FORWARD_OUTCOME`，不进入 C0 或任何正式信号。
- `stock_concept` 涨停天梯：没有上游发布时间，永久不可证明「当时已知」。

## 6. 修复前后对照

| 观测时间 | v2（S4） | v3（S5） |
| --- | --- | --- |
| 14:59 | CLOSE / 已知 | CLOSE / 已知 |
| 15:00 | CLOSE / 已知 | CLOSE / 已知（含边界） |
| 15:01 | CLOSE / 已知（错误） | POST_CLOSE_UNKNOWN / 后得 |
| 15:05 | CLOSE / 已知（错误） | POST_CLOSE_UNKNOWN / 后得 |
| 22:00 | CLOSE / 已知（错误） | POST_CLOSE_UNKNOWN / 后得 |
| D+1 09:00 | LATER / 后得 | LATER / 后得 |
| UTC 06:59 | CLOSE / 已知 | CLOSE / 已知 |
| UTC 07:01 | CLOSE / 已知（错误） | POST_CLOSE_UNKNOWN / 后得 |
| 日线（无 observed_at） | CLOSE / 已知（错误） | END_OF_DAY_FACT / 后得 |

机器可读样例见 `pit_cases.json`。
