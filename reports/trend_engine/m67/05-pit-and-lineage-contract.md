# M6.7 PIT 与血缘实现索引

- 时间契约正文：`04-pit-temporal-contract.md`
- 血缘契约正文：`04-data-lineage-contract.md`
- 泄漏测试规范：`04-pit-leakage-test-spec.md`
- 字段覆盖：`04-pit-field-coverage-matrix.csv`、`05-pit-field-coverage.csv`
- 代码：`data_contracts.PitTimestamps` / `evaluate_pit_eligibility` / `evaluate_data_lineage_completeness` / `classify_data_quality`

## 纯函数清单

| 函数 | 输出 |
| --- | --- |
| `evaluate_pit_eligibility` | `PIT_OK / DATE_LEVEL_ONLY / PIT_UNVERIFIED` + flags |
| `evaluate_snapshot_historical_eligibility` | `HISTORICAL_OK / CURRENT_SNAPSHOT_ONLY` |
| `evaluate_data_lineage_completeness` | `complete`、`missing[]`、`quality_status` |
| `classify_data_quality` | 六态 |

## 结论

所有现有供应商数据集的 `available_at` 均缺失或未说明 → 严格 PIT 一律 `PIT_UNVERIFIED`，不得用于无前视回测证明。
