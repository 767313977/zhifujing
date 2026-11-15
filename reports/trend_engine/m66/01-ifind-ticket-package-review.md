# M6.6 Batch 1：iFinD 工单包复核与答复闭环

- 日期：2026-11-16
- 状态：**`DRAFT_READY_NOT_SENT`**（TRAE 未发送，也不会声称已发送）
- 生成脚本：`scripts/m66_build_ifind_package.py`
- 导入脚本：`scripts/m66_ifind_reply_import.py`
- 测试：`tests/test_trend_m66_replies.py`（6 项通过）
- 网络调用：**0 次**

---

## 1. 28 条问题逐条复核

来源：M6.5 `ifind_vendor_question_matrix.csv`（A 8 + B 8 + C 7 + D 5 = 28）。

复核动作：

| 项 | 处理 |
| --- | --- |
| 编号 | 原样保留 `Q-A1…Q-A8`、`Q-B1…Q-B8`、`Q-C1…Q-C7`、`Q-D1…Q-D5`，不合并、不重排 |
| 证据绑定 | 每条保留 M6.5 的 `current_evidence`，指向 M6.4 报告/样本 |
| 新增 `required_evidence` | 按 A/B/C/D 四类分别定义「拿到什么才算答复」 |
| 新增 `gate_impact` | 映射到 `G9-PRICE / G9-CA / G9-PIT / G3 / G2 / G9-G3` |
| 新增 `reply_status` | 初值全部 `OPEN`；**未答复 ≠ 不支持** |
| 新增 `reply_ref` / `updated_at` | 指向 `provider_reply_register.csv` 的 `reply_id` |

问题数与分区核对：28 / 4 类（A复权、B公司行为、C板块、D许可），与 M6.5 完全一致。

## 2. 发送包内容

目录：`reports/trend_engine/m66/ifind_ticket_package/`

| 文件 | 用途 | 敏感信息扫描 |
| --- | --- | --- |
| `ticket-body.md` | 清洁版工单正文（28 问，按 A/B/C/D 分组） | 无 Token/账号/本地路径 |
| `ifind_28_questions.csv` | 28 条问题 + 证据要求 + 门禁影响 + 状态 | 无 |
| `field_dictionary_template.csv` | 28 个待澄清字段的空模板（字段名/含义/单位/分组/待填答复） | 无 |
| `sample-request-scope.md` | 3 个真实样本的字段行为说明（内部用，发送时只取字段片段） | 无凭据；含内部相对路径，**不直接外发** |
| `sample-response-redaction-template.md` | 答复脱敏清单与登记规则 | 无 |
| `pre-send-checklist.md` | 用户发送前检查表 + 发送后状态更新 | 无 |
| `package_manifest.json` | 包清单、状态、`contains_credentials=false` | 无 |

清洁版正文明确只提 3 只样本证券与「除权日前后 30 个交易日」，不提供完整响应、不做批量导出。

## 3. 答复导入与登记机制

### 3.1 原始答复

- 原始文件复制到 `backend/data/m66_staging/<batch_id>/ifind_reply/<sha16>_<原名>`（gitignore，不入库）
- 登记到 `reports/trend_engine/m66/provider_reply_register.csv`（入库，只含元数据与哈希）
- 同一 SHA256 重复导入 → 返回 `DUPLICATE`，不重复复制、不重复登记
- 已存在的证据文件 → `FileExistsError`，拒绝覆盖

### 3.2 登记字段

`reply_id / supplier / ticket_id / received_at / source_channel / original_filename / stored_path / raw_sha256 / redacted_sha256 / redaction_status / reviewed_by / review_status`

**没有 `available_at` 字段**：`received_at` 只表示我方收到时间，禁止当作供应商可得时间或公告时间（测试 `test_import_records_received_at_not_available_at` 固定该约束）。

### 3.3 敏感内容拦截

若文件命中 `Authorization` / `Bearer` / `api_key=` / `token=` / `password=` / `sk-…` 等模式，且未标记 `redaction_status=DONE`，导入直接失败。

### 3.4 逐题状态

`OPEN / ANSWERED / PARTIAL / NOT_SUPPORTED / NEEDS_FOLLOWUP / VERBAL_UNCONFIRMED`，写入 `question_reply_status.csv`（追加式）。

判定口径：

| 情形 | 状态 |
| --- | --- |
| 只有口头/会议答复 | `VERBAL_UNCONFIRMED` |
| 只给接口名，无字段字典和示例 | `PARTIAL` |
| 明确回复不支持 | `NOT_SUPPORTED` |
| 未答复 | `OPEN`（不得判为不支持） |

## 4. 测试

| 测试 | 覆盖 |
| --- | --- |
| `test_import_records_received_at_not_available_at` | `received_at` 不冒充 `available_at` |
| `test_stored_copy_is_byte_identical` | 证据副本字节一致、哈希一致 |
| `test_duplicate_payload_is_not_stored_twice` | 重复导入识别 |
| `test_sensitive_content_rejected_until_redacted` | 未脱敏含凭据文件拒绝入库 |
| `test_question_status_enum_and_append` | 状态枚举与题目编号校验 |
| `test_invalid_batch_and_channel_rejected` | 批次号/渠道校验 |

结果：**6 passed**。

## 5. Batch 1 验收

| 门禁 | 结果 |
| --- | --- |
| 28 个问题全部可追踪（编号未变） | 通过 |
| 每条含 required_evidence / reply_status / gate_impact | 通过 |
| 工单包无敏感凭据 | 通过（人工复核 + manifest 标记） |
| 未发送状态明确 | 通过（`DRAFT_READY_NOT_SENT`） |
| 原始答复不可覆盖、重复可识别 | 通过（6 项测试） |
| 不因未答复而判定功能不存在 | 通过（默认 `OPEN`） |
