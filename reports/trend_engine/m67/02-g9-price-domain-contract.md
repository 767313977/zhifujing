# M6.7 G9 价格域契约

- 实现：`backend/app/services/trend_engine/data_contracts.py`
- 规则来源：M6.6 实测（1.8883bp、52 行同列、无 `adjust_type`）+ M6.3 双价格域设计
- 本文件只定义契约，不接入任何新数据

---

## 1. 字段定义

| 字段 | 类型 | 单位 | 空值语义 | 说明 |
| --- | --- | --- | --- | --- |
| `security_id` | str | — | 必填 | 统一 `600519.SH` 形式 |
| `trade_date` | str | — | 必填 | `YYYY-MM-DD`，交易日 |
| `raw_open/high/low/close` | float? | 元 | `NOT_PROVIDED` | 不复权；不得用复权值填充 |
| `raw_pre_close` | float? | 元 | `NOT_PROVIDED` | 前收盘；iFinD 返回的 pre_close 已含除权调整，需标注 |
| `adjusted_open/high/low/close` | float? | 元 | `NOT_PROVIDED` | **仅在 `adjustment_type` 明确时允许** |
| `adjustment_factor` | float? | 倍 | `NOT_PROVIDED` | 复权因子原始值 |
| `adjustment_type` | enum? | — | `UNKNOWN` | `RAW / FORWARD / BACKWARD` |
| `factor_direction` | enum? | — | `UNKNOWN` | `FORWARD / BACKWARD` |
| `factor_base_date` | str? | — | `NOT_PROVIDED` | 因子基准日 |
| `factor_cumulative_rule` | str? | — | `NOT_PROVIDED` | 累计规则（如 PRODUCT） |
| `source` / `source_field` | str? | — | 必填 | 溯源到供应商与原始字段名 |
| `source_version` | str? | — | `NOT_PROVIDED` | 供应商或接口版本 |
| `available_at` | str? | — | `PIT_UNVERIFIED` | 数据可供研究使用的时间 |
| `ingested_at` | str? | — | 必填 | 本系统入库时间 |
| `quality_status` | enum | — | — | 六态（见 `02-g9-null-semantics.md`） |
| `is_estimated` | bool | — | `False` | 估算/插补必须置 True |

## 2. 硬规则

1. **`adjusted_*` 必须有 `adjustment_type`**：`validate_price_domain_record` 在 `adjusted_close` 非空而类型为空时给出 `ADJUSTED_WITHOUT_TYPE`，且 `usable_as_adjusted=False`。
2. **RAW 行不得携带 adjusted 值**：`RAW_ROW_CARRIES_ADJUSTED`。
3. **因子语义不全不得构造正式序列**：`validate_adjustment_factor_semantics` 要求 `factor + direction + base_date + cumulative_rule` 四项齐全，否则 `constructable=False`。
4. **`quality_status` 不能从“字段存在”推断**：必须结合 `classify_data_quality(flags, evidence_level)`。

## 3. 与 M6.6 实测的对应

| M6.6 观测 | 契约处理 |
| --- | --- |
| 26 日 `raw×factor` vs 后复权最大 1.8883bp | 记为 `SAMPLE_NUMERIC_MATCH`，**不等于**因子定义通过 |
| 同问前+后复权返回 52 行同列 | 判定 `ADJUSTED_WITHOUT_TYPE`，禁止写入规范化 adjusted |
| 因子 8.6760→8.8814 | 只能说明“实测行为与累计后复权一致”，`factor_direction` 仍需书面确认 |

## 4. 与生产链路的关系

`patterns.build_bars` 生成的“前复权”属于 **`DERIVED_FROM_PCT_CHG`**，在本契约中应映射为：

- `adjustment_type`：`FORWARD`（仅当报告显式声明近似口径时）
- `is_estimated = True`
- `quality_status = PARTIAL`
- `source_field = pct_chg`

不得把该序列当作 `VERIFIED` 的因子复权序列。

## 5. 验收影响

| 条件 | 结果 |
| --- | --- |
| 有官方字段字典 + 方向/基准日/累计规则 | 可进入 G9-PRICE 复核 |
| 仅数值吻合、无字段定义 | 最多 `PARTIAL` |
| 无 `adjustment_type` | 不得 PASS |
