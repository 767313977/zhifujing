# M6.2 Batch 2：历史证券主数据设计

- 目标：把「证券是谁、什么时候是什么状态、属于什么行业、多大市值」变成可追溯的时间序列，替代当前快照回填。
- 本批次只设计，不建表、不回填。
- 现状证据：`pit_inventory.json`、`data_catalog.json`。

---

## 1. 现状证据（b1_research.db）

| 事实 | 数值 | 含义 |
| --- | --- | --- |
| `stock_daily` 覆盖代码 | 5,303 | 全部为区间内仍在交易的代码 |
| 每只代码的最后交易日 | 最早 2026-09-03 | **没有任何代码在区间内退市**（否则会出现更早的末日） |
| 30 个交易日以上无行情的代码 | 0 | 池内无长期停牌/退市残留 |
| 名称含 `ST` 的代码 | 140（67,909 行） | 名称来自逐日抓取，但无公告时间 |
| 区间内发生更名的代码 | 7 | 见下表；说明 `name` 会随抓取更新，但无法证明当时何时可见 |
| `stock_basic.asof` | 单一快照 | 行业、总市值、流通市值均为当前值，**非 PIT** |
| `stock_universe` | 5,261 行、单一 `updated_at` | 当前证券池，**非 PIT** |

更名样本（`stock_daily` 内名称变化）：

| 代码 | 旧名 → 新名 | 旧名区间 | 新名起点 |
| --- | --- | --- | --- |
| 300096 | ST易联众 → 易联众 | 2024-09-02 ~ 2026-09-23 | 2026-09-28 |
| 300527 | ST应急 → 中船应急 | 2024-09-02 ~ 2026-09-28 | 2026-09-30 |
| 301139 | *ST元道 → 元道退 | 2024-09-02 ~ 2026-08-28 | 2026-09-30 |
| 600363 | 联创光电 → ST联光 | 2024-09-02 ~ 2026-09-28 | 2026-09-30 |
| 301339 | 通行宝 → 苏交数智 | 2024-09-02 ~ 2026-09-28 | 2026-09-29 |
| 301716 | 鸿富诚 → C鸿富诚 → 鸿富诚 | 2026-09-29 / 09-30 | 新股上市前缀 |
| 301139 | 元道退 | — | 退市整理标识，但区间内仍在交易 |

结论：

1. `stock_daily.name` 是「抓取当日名称」，但**没有保存公告时间**，且有被后续刷新覆盖的风险 → 不能证明历史 ST 状态。
2. 无退市股意味着研究池存在**幸存者偏差**；不能因为「没找到」就认为「不存在」。
3. 行业、市值不可用于历史中性化。

---

## 2. 目标表设计（新增，不改原表）

### 2.1 `security_master`（证券主档）

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `code` | TEXT PK | 6 位代码 |
| `exchange` | TEXT | SSE / SZSE / BSE |
| `list_date` | DATE | 上市日 |
| `delist_date` | DATE NULL | 退市日 |
| `board` | TEXT | 主板 / 创业板 / 科创板 / 北交所 |
| `is_active` | BOOLEAN | 当前是否在市（仅作便捷列，不作为 PIT 依据） |
| `source`, `source_version`, `revision_id`, `is_final`, `supersedes_id`, `payload_hash` | 见契约 | 修订链 |

### 2.2 `security_status_history`（ST / 停牌 / 退市整理）

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `code` | TEXT | |
| `status` | TEXT | `NORMAL` / `ST` / `*ST` / `SUSPENDED` / `DELISTING` / `DELISTED` |
| `effective_date` | DATE | 状态生效日 |
| `available_at` | TIMESTAMP | 公告可得时间（未知写 NULL） |
| `announcement_id` | TEXT NULL | 公告编号 |
| `source` … `payload_hash` | 契约字段 | |
| PK | `(code, status, effective_date, revision_id)` | |

要求：

- 状态必须用**公告**驱动，禁止用当前名称反推；
- 同一代码允许状态区间重叠（如停牌期间 ST 生效），查询时按 `status` 分别取；
- `available_at IS NULL` 的区间只能用于事后描述。

### 2.3 `security_name_history`

| 列 | 说明 |
| --- | --- |
| `code`, `name`, `effective_date`, `available_at`, `change_reason` | 更名原因（ST 摘帽、重组、上市前缀等） |
| 契约字段 | 同上 |

### 2.4 `industry_membership_history`

| 列 | 说明 |
| --- | --- |
| `code`, `industry_code`, `industry_name`, `taxonomy`, `taxonomy_version` | 分类体系必须带版本（申万 2021 / 东财 / 同花顺…） |
| `effective_date` / `available_at` | 调整生效日与披露时间 |
| `is_primary` | 是否主分类 |
| 契约字段 | 同上 |

### 2.5 `market_cap_daily`（历史市值快照）

| 列 | 说明 |
| --- | --- |
| `code`, `trade_date` | 日频 |
| `total_mv`, `float_mv`, `free_float_mv` | 单位统一为元 |
| `available_at` | 收盘后可得时间 |
| 契约字段 | 同上 |

用途：市值分层、行业中性化、流动性过滤。当前 `stock_basic.total_mv` 只有单一快照，不可用。

### 2.6 `delisting_event`

| 列 | 说明 |
| --- | --- |
| `code`, `delist_date`, `reason`, `announcement_date`, `available_at` | 退市原因与公告时间 |
| `last_trade_date`, `is_forced` | 便于构造「退市前样本」 |

---

## 3. PIT 查询示例

```sql
-- asof 当日该股票是否 ST
SELECT status
FROM security_status_history
WHERE code = :code
  AND effective_date <= :asof
  AND (available_at IS NULL OR available_at <= :asof_ts)
ORDER BY effective_date DESC, available_at DESC
LIMIT 1;

-- asof 当日的行业（缺失即 UNKNOWN，禁止回退当前行业）
SELECT industry_code
FROM industry_membership_history
WHERE code = :code AND effective_date <= :asof AND available_at <= :asof_ts
ORDER BY effective_date DESC LIMIT 1;
```

缺失处理：

| 情况 | 返回 |
| --- | --- |
| 无记录 | `UNKNOWN`，该股票在行业分层中归入 `UNMAPPED` 并从信号中排除 |
| 有记录但 `available_at` 为空 | `UNKNOWN_PIT`，不得用于过滤 |
| 分类体系版本不一致 | 拒绝混用，按版本分别统计 |

---

## 4. 迁移与验证计划（需授权）

| 步骤 | 内容 | 验证 |
| --- | --- | --- |
| 1 | 从交易所公告 / 数据商 `namechange` 拉取更名与 ST 区间 | 抽样 20 只人工核对，与现有 7 条更名记录对齐 |
| 2 | 拉取 `stock_basic(list_status=D)` 与退市公告，构造 `delisting_event` | 退市股数量 > 0，且能在 `stock_daily` 里找到对应末日 |
| 3 | 拉取历史行业成分（带 in/out 日期） | 任选 3 个日期，行业分布与当时研报一致 |
| 4 | 拉取日频 `daily_basic` 市值 | 与 `stock_basic` 当前快照在最后一日误差 < 1% |
| 5 | 用新表重算 M6.1 的行业/市值分层 | 与旧结果差异必须能解释（旧值非 PIT） |

---

## 5. 明确不可用项

| 项 | 状态 | 原因 |
| --- | --- | --- |
| 历史 ST 状态（可证明） | 不可用 | 现库无公告时间 |
| 历史行业 | 不可用 | 仅当前快照 |
| 历史市值 | 不可用 | 仅当前快照 |
| 退市股样本 | 不可用 | 池内 0 只 |
| 更名的公告可得时间 | 不可用 | 现库无公告记录 |

以上任一项在补齐前，相关分层只能标注为「事后描述」，不得作为选股条件或风险过滤依据。
