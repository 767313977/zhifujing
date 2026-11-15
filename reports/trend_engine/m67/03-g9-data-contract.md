# M6.7 G9 数据契约实现索引

- 代码：`backend/app/services/trend_engine/data_contracts.py`
- 测试：`tests/test_trend_m67_g9_contract.py`（18 项）
- 矩阵：`03-g9-validation-matrix.csv`
- 说明文档：`02-g9-price-domain-contract.md`、`02-g9-corporate-action-contract.md`、`02-g9-null-semantics.md`

## 1. 类型

| 类型 | 用途 |
| --- | --- |
| `PriceRecord` | 单证券单日价格（raw / adjusted / 因子元数据） |
| `CorporateActionRecord` | 公司行为事件（分红/送转/配股/未决） |
| `PitTimestamps` | 五类时间（effective / announced / available / ingested / retrieved） |
| `MissingValue` / `QualityStatus` / `AdjustmentType` / `FactorDirection` / `EventType` | 枚举 |

## 2. 纯校验器

| 函数 | 作用 | 关键输出 |
| --- | --- | --- |
| `classify_missing_value` | 六态空值分类 | `MissingValue` |
| `validate_adjustment_factor_semantics` | 因子方向/基准日/累计规则 | `constructable`、`flags` |
| `validate_price_domain_record` | 价格域分离 | `quality_status`、`usable_as_adjusted` |
| `validate_corporate_action_record` | 事件字段与空值 | `field_status`、`flags` |
| `validate_event_temporal_order` | 公告≤登记≤除权≤派息 | `issues` |
| `evaluate_pit_eligibility` | PIT 可用性 | `PIT_OK / DATE_LEVEL_ONLY / PIT_UNVERIFIED` |
| `evaluate_snapshot_historical_eligibility` | 历史截面资格 | `HISTORICAL_OK / CURRENT_SNAPSHOT_ONLY` |
| `evaluate_data_lineage_completeness` | 血缘完整度 | `complete`、`missing` |
| `classify_data_quality` | 汇总质量 | 六态 |
| `mapping_is_approved` | 板块映射审批 | `approved`、`candidate_only` |

## 3. 设计约束

1. 全部纯函数：无 I/O、无网络、无数据库；
2. 返回结构化 dict，不抛业务异常（便于批量汇总）；
3. 不使用 `datetime.now()`，确保确定性；
4. 不依赖 pandas/numpy，避免隐式类型转换。

## 4. 与生产代码的边界

- 本模块**不修改** `patterns.build_bars`、`trade_sim`、`sector_strength`；
- 生产信号价仍走 `build_bars`（`DERIVED_FROM_PCT_CHG`）；
- 是否接入本契约，留待 M6.8 评审（见 `02-trend-engine-data-flow-audit.md` R-01/R-02）。
