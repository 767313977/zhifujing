# M6.7 数据血缘契约

实现：`data_contracts.evaluate_data_lineage_completeness`

---

## 1. 每条规范化记录必须可追溯

| 维度 | 字段 | 缺失后果 |
| --- | --- | --- |
| 来源 | `provider`、`dataset`、`source_field` | 无法回溯 → `PARTIAL` |
| 标识 | `source_record_id` / `source_event_id` | 无法去重与审计 |
| 单位 | `source_unit`、`canonical_unit` | 单位混淆 |
| 转换 | `transform_version` | 无法复现 → `PARTIAL` |
| 批次 | `batch_id` | 无法定位采集轮次 |
| 摘要哈希 | `payload_hash` | 仅摘要，不能完整重放 |
| 时间 | `ingested_at`、`available_at` 证据 | PIT 不可用 |
| 校验 | `quality_status`、`flags` | 质量不可判 |
| 人工 | `review_status`、`reviewed_by` | 未复核数据被当已复核 |

## 2. 摘要证据的局限

M6.6 未获准持久化原始 payload，仅保存哈希与结构摘要。因此：

- **可以**证明「某次请求返回了哪些字段、多少行、哈希是多少」；
- **不能**宣称「可完整重放供应商响应」；
- 任何需要逐字节复现的场景，必须等许可确认后重新采集。

## 3. 转换版本规则

- 归一化规则变更必须升 `transform_version`；
- 同一 `batch_id` 内不得混用两个转换版本；
- 归一化结果必须保留 `source_record_id` 列表。

## 4. 与生产缓存的关系

`cache_identity.build_cache_key`（`cache_identity.py:17`）已定义四维键（db_sha256 / code_sha / params / data_version），但**尚未接入现有缓存**（M6.7 N2 R-10）。接入前不得宣称缓存可复现。
