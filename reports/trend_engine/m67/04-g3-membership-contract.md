# M6.7 G3 成分契约实现索引

- 代码：`data_contracts.SectorMembershipSnapshot` / `SectorProviderMapping` / `evaluate_snapshot_historical_eligibility` / `mapping_is_approved`
- 测试：`tests/test_trend_m67_g3_contract.py`（10 项）
- 契约正文：`03-g3-historical-membership-contract.md`
- 能力/许可矩阵：`03-provider-g3-capability-matrix.csv`、`03-provider-g3-license-matrix.csv`

## 1. 判定链

```
provider 返回
  → is_current_snapshot?
      True  → CURRENT_SNAPSHOT_ONLY（禁止入历史池）
      False → date_param_honored?
                False → CURRENT_SNAPSHOT_ONLY（日期被忽略）
                True  → available_at <= snapshot_date?
                          False → AVAILABLE_AFTER_SNAPSHOT_DATE
                          True  → HISTORICAL_OK
```

## 2. 映射审批

- `name_exact_candidate` / `name_fuzzy_candidate` / `name_unverified` → 一律 `pending`，`candidate_only=True`；
- 只有 `official_code_table` / `manual_reviewed` / `exact_code_match` **且** `review_status=approved` 才通过。

## 3. 与 staging 护栏的复用

- `multisource.assert_snapshot_mode`：`SNAPSHOT_ONLY` 不得带 in/out；
- `multisource.assert_snapshot_not_from_current`：禁止当前快照回填历史。

## 4. 未实现项（明确记录）

| 项 | 原因 |
| --- | --- |
| in/out 区间重叠校验 | 无真实 in/out 数据 |
| 退市证券保留校验 | 无历史快照 |
| 非交易日截面规则 | 需与供应商确认「取前一交易日」语义 |
