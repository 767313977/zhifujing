# M6.4 Batch 5：Staging 数据库与幂等落地

- 位置：`backend/data/m64_staging/batch_20261115T152701Z/m64_ifind_staging.db`（创建前不存在）
- 证据：`ingestion_manifest.json`、`api_usage_before_after.json`
- 结论：**staging 建成；原始库 SHA 未变；G9 相关表为空并记录原因。**

---

## 1. 表与行数

| 表 | 行数 | 说明 |
| --- | ---: | --- |
| `provider_manifest` | 1 | iFinD / HTTP MCP / SDK NOT_INSTALLED |
| `raw_payload_manifest` | 9 | staging 原始 JSON 的 SHA256 |
| `index_daily` | 132 | 6 指数 × 22 日历行（含空行，close 非空的为有效） |
| `sector_daily_raw` | 46 | 2 个板块的长表指标 |
| `price_factor` | **0** | 字段语义不清，拒绝写入 |
| `corporate_action` | 3 | 仅有日期，金额/公告时间为 NULL |
| `ingestion_run` | 1 | calls=17、cells≈1970、status=PARTIAL |
| `ingestion_error` | 4 | 因子缺失、公告缺失、派现缺失、无成分接口 |

---

## 2. 元数据规则

每条记录均带：

- `source=iFinD`、`source_function`、`source_version=ifind_mcp_20261115`
- `available_at`：供应商未提供 → **NULL**（不用 `ingested_at` 顶替）
- `ingested_at`：本批次实际抓取时间
- `payload_hash`：规范化行/文件 SHA256
- `quality_status`：`OK` / `PARTIAL` / `UNKNOWN_*`

---

## 3. 幂等与修订

| 规则 | 实现 |
| --- | --- |
| 重复导入 | `INSERT OR REPLACE`，主键 `(batch_id, code, trade_date)` 等 |
| 修订保留 | 不同 `batch_id` 分开存放；不覆盖历史批次 |
| 失败回滚 | 单批次独立文件；失败可直接删除该批次目录，不影响其他批次 |
| 原始响应 | 保存到 staging，不提交 Git；报告只引用 SHA |
| 字段冲突 | 进入 `ingestion_error`（本轮 4 条） |

---

## 4. 身份

| 项 | 值 |
| --- | --- |
| 源库 SHA256 before/after | 一致（`source_db_unchanged: true`） |
| staging SHA256 | 见 `ingestion_manifest.json` |
| 本轮调用 / 单元格 | 19 次 / ≈2,230（预算 50,000，占 ≈4.5%） |

---

## 5. 未落地数据

- `price_factor`：0 行（字段方向/基准未验证）；
- `corporate_action`：只有日期，**不含任何金额或股份比例**；
- 板块成分：无表数据（无接口）。

这些空表是**有意的拒绝**，不是遗漏；对应的 `ingestion_error` 可供审计。
