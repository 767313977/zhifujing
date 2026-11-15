# M6.7 G9 公司行为契约

- 实现：`data_contracts.CorporateActionRecord` + `validate_corporate_action_record`
- 事实来源：M6.6 实测（分红字段为空、300176 配股完整、300750 未决）

---

## 1. 字段定义

| 字段 | 类型 | 单位 | 空值语义 |
| --- | --- | --- | --- |
| `event_id` | str | — | 必填（本系统生成） |
| `security_id` | str | — | 必填 |
| `event_type` | enum | — | `CASH_DIVIDEND / BONUS_SHARE / TRANSFER / RIGHTS_ISSUE / SPLIT / MERGE / UNRESOLVED` |
| `cash_dividend_per_share_gross` | float? | 元/股（税前） | `NOT_PROVIDED` 或 `EXPLICIT_ZERO` |
| `bonus_share_ratio` | float? | 股/股 | 同上 |
| `transfer_share_ratio` | float? | 股/股 | 同上 |
| `rights_issue_ratio` | float? | 股/股 | 同上 |
| `rights_issue_price` | float? | 元/股 | 同上 |
| `rights_issue_shares` | float? | 股 | 同上 |
| `announcement_date` | str? | 日期 | 日期级 |
| `announcement_timestamp` | str? | 时间 | **缺失时保持 NULL** |
| `record_date` / `ex_date` / `payment_date` / `share_listing_date` | str? | 日期 | 各自独立 |
| `revision_status` | str? | — | `ORIGINAL / REVISED / CANCELLED` |
| `cancelled_at` | str? | — | 取消记录保留原行 |
| `available_at` / `ingested_at` | str? | — | 见 PIT 契约 |
| `source` / `source_event_id` | str | — | 必填 |
| `missing_reasons` | dict | — | 逐字段说明缺失原因 |
| `quality_status` | enum | — | 六态 |

## 2. 硬规则

1. **NULL ≠ 0**：`0` 是有效值（`EXPLICIT_ZERO`）；`None` 是缺失（`NOT_PROVIDED`）。测试 `test_null_zero_and_not_applicable_are_distinct` 固定。
2. **事件类型未决不得自动分类**：`event_type=UNRESOLVED` → `quality_status=UNKNOWN`，并带 `EVENT_TYPE_UNRESOLVED`。
3. **现金分红缺金额不得判 VERIFIED**：`CASH_AMOUNT_MISSING`。
4. **配股三项独立**：比例、价格、实际股数任一缺失都单独报 `RIGHTS_FIELD_MISSING:<field>`，禁止互相推导。
5. **公告日期不得补时间戳**：`2026-06-26T00:00:00` 会触发 `DATE_PADDED_AS_TIMESTAMP`。
6. **取消事件保留原行**：`CANCELLED_EVENT_RETAINED`，不得删除。
7. **时间顺序**：公告 ≤ 登记 ≤ 除权 ≤ 派息（`validate_event_temporal_order`）。

## 3. 固定疑难样本

| 样本 | 契约中的状态 | 说明 |
| --- | --- | --- |
| 600519 现金分红 | `CASH_AMOUNT_MISSING` | iFinD 返回空值；等待书面答复或其他供应商 |
| 300176 配股 | `VERIFIED`（三项齐全） | 公告 2026-08-10、除权 2026-08-21、比例 0.4、价格 3.36、实际 1.4417 亿股；`record_date` 缺失 |
| 300750 2026-04-22 | `UNRESOLVED` + `UNKNOWN` | 不沿用 M6.4「配股」结论；需第二来源裁决 |

## 4. 多来源与重复记账

- 同一事件多条来源记录：以 `(security_id, event_type, record_date, ex_date)` 为事件键；
- 若同一键下存在多个不同 `plan_id`，或均无 `plan_id`，标 `CONFLICT_MULTI_PLAN`，**不自动求和**（复用 M6.6 `m66_g9_validator.aggregate_events` 规则）；
- 归一化结果必须记录所依据的 `source_event_id`。

## 5. 验收影响

| 条件 | 结果 |
| --- | --- |
| 现金分红、送转、配股均有实际值与单位 | 可进入 G9-CA 复核 |
| 只有配股一项完整 | G9-CA 维持 FAIL |
| 无公告时间 | G9-PIT 维持 `PIT_UNVERIFIED` |
