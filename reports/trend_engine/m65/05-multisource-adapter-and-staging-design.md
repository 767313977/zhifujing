# M6.5 Batch 5：多供应商适配器与独立 staging 设计

- 日期：2026-11-15
- 实现：`backend/app/services/trend_engine/multisource.py`
- Schema 草案：`reports/trend_engine/m65/m65_staging_schema.sql`（由 `scripts/m65_write_schema_draft.py` 从代码常量生成，防止漂移）
- 测试：`tests/test_trend_m65_provider.py`（9 项）、`tests/test_trend_m65_staging.py`（12 项）、`tests/test_trend_m65_guard.py`（7 项），合计 28 项通过
- 网络：**零调用**。本模块纯内存/本地 SQLite，不发起任何请求。

---

## 1. 设计边界

- 只定义**协议与落库规则**，不为未授权供应商编写任何「看起来可用」的真实 API 实现。
- iFinD 的既有 HTTP MCP 客户端保持原样（M6.4 已用），本模块不复制、不替换它。
- staging 与四个原始研究库彻底隔离：路径校验 + 文件名黑名单，任何写操作前先拒绝。

## 2. 协议

```python
class CorporateActionProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...
    def get_corporate_actions(self, request: CorporateActionRequest) -> ProviderResult: ...
    def get_adjustment_factors(self, request: FactorRequest) -> ProviderResult: ...

class SectorMembershipProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...
    def get_sector_snapshot(self, request: SectorSnapshotRequest) -> ProviderResult: ...
    def get_sector_membership_events(self, request: MembershipEventRequest) -> ProviderResult: ...
```

- `ProviderCapabilities` 强制校验供应商名与 `pit_precision ∈ {date, timestamp, none}`，防止「时间戳级」被随口声明。
- `NotImplementedProvider` 对四个取数方法全部抛 `ProviderNotImplemented`，`capabilities().datasets == ()`，`license_scope = NOT_AUTHORIZED`——未接入的供应商**不可能**返回成功。
- `ProviderRegistry.get()` 三重拒绝：未知供应商、未授权、已授权但未注册适配器。默认授权集合仅 `{"iFinD"}`。

## 3. 统一元数据

每条落库记录都带（对应任务书 7.2）：

`provider`、`product`、`endpoint`、`field_code`、`provider_version`、`available_at`、`announcement_date`、`ingested_at`、`revision_id`、`payload_hash`、`quality_status`、`license_scope`、`batch_id`。

两条硬规则：

1. `available_at` 缺失写 NULL。`normalize_available_at("")` 返回 `None`，不允许用 `snapshot_date`/`trade_date`/`ingested_at` 顶替。
2. 公告时间只存日期级。`normalize_announcement("2026-06-26T00:00:00+08:00")` → `("2026-06-26", "date")`，**不会**补时间戳。即使供应商返回时分秒，也降级为 date-level。

## 4. 独立 staging

- 位置：`backend/data/m65_staging/<batch_id>/m65_multisource_staging.db`
- 批次目录已存在即 `StagingViolation`（不覆盖）；`batch_id` 只允许 `[0-9A-Za-z_-]`。
- 12 张表：`provider_manifest`、`provider_capability`、`raw_payload_manifest`、`corporate_action_raw`、`adjustment_factor_raw`、`sector_master_raw`、`sector_membership_snapshot`、`sector_membership_event`、`sector_id_crosswalk`、`ingestion_run`、`ingestion_error`、`license_manifest`。

关键约束：

| 表 | 约束 |
| --- | --- |
| `raw_payload_manifest` | `UNIQUE(batch_id, provider, endpoint, payload_hash)`，重复写入直接 `IntegrityError`（只追加） |
| `corporate_action_raw` | 主键含 record_id；**不同 provider 同代码同日事件各存一行**，不互相覆盖 |
| `adjustment_factor_raw` | 同上；`factor_type` 默认 `UNKNOWN`，禁止猜测前/后复权 |
| `sector_membership_snapshot` | 带 `membership_mode` 与 `provider_asof` |
| `sector_id_crosswalk` | 主键 `(batch_id, b1_sector_id, provider)`，`review_status` 决定能否用于归一化 |
| `ingestion_error` | 供应商错误码、权限拒绝、限流必须落表，不吞 |

## 5. 版本与冲突

- 原始响应只追加；修订以新 `record_id` 追加，不 UPDATE。
- 同一事件多供应商保留多条来源记录，归一化结果必须记录 `source_record_id`。
- 字段冲突不自动裁决，进入 quarantine（后续批次实现，本阶段先保证原始层可追溯）。
- 未确认授权的原始数据**不得持久化**：`license_scope` 默认 `NOT_AUTHORIZED`，写入前由调用方显式声明。

## 6. 护栏 → 测试映射

| # | 护栏 | 实现 | 测试 |
| ---: | --- | --- | --- |
| 1 | 权限错误不当空数据 | `ProviderResult` 空结果需 `empty_confirmed=True` | `test_empty_result_requires_explicit_confirmation`、`test_provider_error_code_is_not_empty_data` |
| 2 | 未授权 provider 硬拒绝 | `ProviderRegistry.get` | `test_unauthorized_providers_hard_rejected` |
| 3 | 来源与哈希保留 | `raw_payload_manifest` | `test_raw_payload_append_only_and_provider_isolated` |
| 4 | 多供应商不互相覆盖 | 记录级主键 + provider 维度 | `test_multi_provider_same_event_not_overwritten` |
| 5 | `available_at` 不伪造 | `normalize_available_at` | `test_available_at_missing_stays_null` |
| 6 | 公告日期不转时间戳 | `normalize_announcement` | `test_announcement_date_is_never_promoted_to_timestamp` |
| 7 | `SNAPSHOT_ONLY` 不伪装区间 | `assert_snapshot_mode` | `test_snapshot_only_cannot_carry_interval` |
| 8 | 模糊映射不自动通过 | `make_mapping` / `require_approved_mapping` | `test_fuzzy_sector_mapping_requires_review` |
| 9 | 历史成分不用当前快照回填 | `assert_snapshot_not_from_current` | `test_snapshot_cannot_backfill_from_current` |
| 10 | 不生成/执行合并 DDL/DML | 无合并脚本；guard 扫描 | `test_no_index_staging_merge_artifacts`、`test_m65_scripts_do_not_write_research_dbs` |
| 11 | 失败批次不影响其他批次 | 独立目录 + 拒绝覆盖 | `test_failed_batch_does_not_affect_other_batches` |
| 12 | 四个原始库 SHA 不变 | 与 `baseline_freeze.json` 比对 | `test_protected_databases_unchanged` |
| 13 | 未授权第三方硬拒绝 | 同 #2，覆盖四家候选 | `test_unauthorized_providers_hard_rejected` |
| 14 | 不产出指数合并审批包 | guard 扫描 m65 目录 | `test_no_index_staging_merge_artifacts` |

## 7. 未接入供应商

| 供应商 | 适配器状态 | 说明 |
| --- | --- | --- |
| iFinD | 既有 HTTP MCP 客户端（不改动） | 仅行情/指数；无成分与公司行为明细 |
| Tushare | `NotImplementedProvider("Tushare")` | 需 2000/6000 积分与授权 |
| Lixinger | `NotImplementedProvider("Lixinger")` | 需 API Token |
| CnOpenData | `NotImplementedProvider("CnOpenData")` | 文件交付，无实时 API |
| Choice | `NotImplementedProvider("Choice")` | 需终端激活 + 套餐 |
| JoinQuant | `NotImplementedProvider("JoinQuant")` | 备选，未授权 |

## 8. 与 DDL/DML 禁令的一致性

- staging 建表只发生在**新建批次目录**内的 `m65_multisource_staging.db`；`assert_not_research_db` 在写前拦截四个库名。
- `scripts/m65_*.py` 中不存在 `INSERT INTO index_daily`、`DELETE FROM` 或 `mode=rw`（证据：`test_m65_scripts_do_not_write_research_dbs`）。
- 本阶段不生成任何「把指数 staging 写进 b1」的 SQL、脚本或审批包。

## 9. 使用示例（伪代码，未执行）

```python
registry = ms.ProviderRegistry()          # 默认只授权 iFinD
adapter = registry.get("iFinD")           # 其他供应商在此处即被拒绝

batch = ms.StagingBatch(root, batch_id, now=iso_now)
record_id = batch.stage_raw("iFinD", "index_data", payload)
batch.stage_factor({...})                 # available_at 缺失自动写 NULL
batch.stage_snapshot({...})               # SNAPSHOT_ONLY 不允许带 in/out
batch.close()
```

## 10. 下一步

- 用户批准某供应商后，为其编写**独立适配器模块**（例如 `multisource_tushare.py`），并把 `ProviderRegistry(allowed={...})` 的放行范围写进审批记录。
- 适配器接入后必须补：真实错误码 → `ProviderError`、限流 → `ingestion_error`、字段缺失 → `SCHEMA_MISMATCH`。
