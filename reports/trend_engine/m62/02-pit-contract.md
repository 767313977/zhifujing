# M6.2 Batch 2：统一 PIT 数据契约

- 目的：为每一类研究数据定义「何时生效 / 何时可知 / 何时入库 / 是否被修订」，使任何历史回测都能证明「当时只能看到这些值」。
- 本批次**只做设计**：不建表、不写库、不回填。契约落地需另行授权。
- 盘点依据：`data_catalog.json`、`pit_inventory.json`（均为只读扫描产物）。

---

## 1. 十条契约字段

| 字段 | 类型 | 语义 | 取值规则 |
| --- | --- | --- | --- |
| `effective_date` | DATE | 业务事实生效日 | 行情=交易日；财务=报告期；成分=调整生效日；ST=状态生效日 |
| `available_at` | TIMESTAMP | 市场参与者**最早可获得**时间 | 交易所收盘后披露 ≈ 15:30–18:00；公告=公告时间；**缺失时写 `UNKNOWN`，禁止用 `effective_date` 顶替** |
| `ingested_at` | TIMESTAMP | 本系统实际采集入库时间 | 取采集器写入时间（`collect_log.created_at` 或新增列）；历史回补数据必须写「回补时刻」，不得写业务日期 |
| `source` | TEXT | 数据来源标识 | 如 `tushare.daily`、`akshare.em_sector`、`exchange.announcement`、`internal.compute` |
| `source_version` | TEXT | 源版本 / 接口版本 / 采集器版本 | 采集器 `git sha` 或供应商标注的版本号 |
| `revision_id` | TEXT | 本次修订标识 | 同一 `(dataset, key, effective_date)` 的多次入库形成链 |
| `is_final` | BOOLEAN | 是否最终版本 | 供应商标记 final / 已过修订观察窗（如 T+3）才置 1 |
| `supersedes_id` | TEXT | 被本记录替代的上一条 | 首版为空；修订时指向旧 `revision_id` |
| `quality_status` | TEXT | 质量状态 | `OK` / `PARTIAL` / `STALE` / `SUSPECT` / `UNKNOWN` |
| `payload_hash` | TEXT | 原始载荷摘要 | 对规范化后的原始记录做 SHA256，用于证明未被静默改写 |

### 1.1 核心约束

1. `effective_date` ≠ `available_at`。禁止把交易日当作可交易时点。
2. 任何 PIT 查询必须写成 `available_at <= :asof AND (asof 时点 is_final 的最新 revision)`。
3. `available_at = UNKNOWN` 的记录**只能做描述性研究**，不得进入选股条件。
4. 修订不得原地覆盖：新记录 + `supersedes_id` 链，保留旧值。
5. `ingested_at` 用于识别「回补数据」：若 `ingested_at > 业务日 + N 天`，该字段在早期区间只能标注为「回补值，非当时可见」。

---

## 2. 现状映射：已有表缺什么

| 数据类 | 现有表 | 现有日期列 | 现有来源/时间戳 | 缺失的契约字段 | PIT 判定 |
| --- | --- | --- | --- | --- | --- |
| 个股日线 | `stock_daily` | `trade_date` | 无 | `available_at`、`ingested_at`、`source`、`revision_id`、`payload_hash` | **不可证明**（只有事实，无可见时间） |
| 指数日线 | `index_daily` | `trade_date` | 无 | 同上 | 不可证明，且比个股晚 5 个交易日 |
| 板块日线 | `sector_daily` | `trade_date` | `source` | `available_at`、`ingested_at`、`revision_id` | 不可证明 |
| 板块成分 | `sector_member` | `trade_date` | `fetched_at` | `available_at`（`fetched_at` 是抓取时间，不等于披露时间）、`revision_id` | 部分：有抓取时刻，但只有 60 个交易日 |
| RPS | `rps_daily` | `trade_date` | 无 | 算法版本、输入截止日、`source_version` | 不可证明；存在混版本日 2026-10-21 |
| 市场宽度 | `breadth_daily` | `trade_date` | 无 | 全部 | 空表 |
| 交易日历 | `trade_calendar` | `trade_date` | 无 | 版本、来源 | 可用作日期轴，非行情 |
| 行业 / 市值 | `stock_basic` | `asof`（快照） | `updated_at` | 历史序列、`effective_date`、`available_at` | **非 PIT**：当前快照回填历史 |
| 证券池 | `stock_universe` | 无 | `updated_at` | `effective_date`、`available_at` | 非 PIT：当前池 |
| ST 状态 | `stock_daily.name` | `trade_date` | 无 | 状态生效日、公告可得时间、来源 | **不可证明**：名称逐日更新，但无公告时间，且可能被后续刷新覆盖 |
| 退市 | 无 | — | — | 全部 | **缺失**：末日最新为 2026-09-03，说明池内无退市股 |

`data_catalog.json` 显示：`stock_daily`、`index_daily`、`rps_daily`、`breadth_daily` 四张核心表**没有任何时间戳列**；全库只有 `collect_log`（3139 行）可间接推断部分采集时间。

---

## 3. 分数据类的 `available_at` 判定规则（设计）

| 数据类 | `effective_date` | `available_at` 规则 | 依据 |
| --- | --- | --- | --- |
| 个股/指数/板块日线 | 交易日 | 收盘后首次可获取时间；优先取采集器时间戳，否则 `UNKNOWN` | 交易所 15:00 收盘，行情文件随后发布 |
| 涨跌停池 / 龙虎榜 | 交易日 | 交易所/数据商发布时刻（通常收盘后至次日盘前） | 需保存原始发布时间 |
| 板块成分 | 调整生效日 | 数据商披露时刻；无法获得的区间 → `UNKNOWN` | 成分调整非每日披露 |
| 财务数据 | 报告期 | 公告披露时间（披露日盘后/次日） | 必须用公告日，不得用报告期 |
| ST / 更名 | 状态生效日 | 公告时间（停牌一日复牌前） | 需交易所公告或 `namechange` 的公告日 |
| 退市 | 终止上市日 | 公告时间 | 需交易所公告 |
| 行业 / 市值 | 交易日 | 收盘后 | 日频快照可构造 PIT 序列 |
| RPS / breadth | 交易日 | 由本系统计算 → 计算完成时间 = `available_at` | 自算数据必须记录输入截止日与代码版本 |

---

## 4. 查询模板（未来实现）

```sql
-- 取 asof 时点可见的最新版本
WITH visible AS (
  SELECT *,
         ROW_NUMBER() OVER (
           PARTITION BY code, trade_date
           ORDER BY available_at DESC, revision_id DESC
         ) AS rn
  FROM stock_daily_pit
  WHERE available_at IS NOT NULL
    AND available_at <= :asof
)
SELECT * FROM visible WHERE rn = 1;
```

禁止写法（现状常见）：

```sql
-- 错误：把业务日当可见日，等价于使用未来数据
SELECT * FROM stock_daily WHERE trade_date <= :asof;
```

---

## 5. 质量状态枚举

| 值 | 含义 | 下游处理 |
| --- | --- | --- |
| `OK` | 契约字段齐全、值与源一致 | 可用于研究 |
| `PARTIAL` | 缺 `available_at` 等关键字段 | 只做描述性统计 |
| `STALE` | 覆盖率不足或日期滞后（如指数晚 5 日） | 相关特征返回 `UNKNOWN` |
| `SUSPECT` | 混版本、重复、异常值 | 拒绝读取该日期 |
| `UNKNOWN` | 无法判定 | 不得进入信号 |

---

## 6. 落地顺序（需授权后执行）

1. 先为 `stock_daily` / `index_daily` / `sector_daily` 增加 `available_at`、`ingested_at`、`source`、`source_version`、`payload_hash`（新增表，不改原表）。
2. 用 `collect_log` 能对上的日期回填 `ingested_at`；对不上的一律 `UNKNOWN`。
3. 再补证券主数据（见 `03-security-master-history-design.md`）。
4. 最后才是 RPS / breadth 的版本化重算（见 `07-rps-versioning-and-quality.md`）。
5. 任何一步都必须在独立研究库执行，禁止直接改生产库。

---

## 7. 本批次结论

| 项 | 结论 |
| --- | --- |
| 契约字段 | 已定义 10 个字段及语义 |
| 现状差距 | 核心 4 张表无任何可见时间/版本字段；快照类字段为非 PIT |
| 可立即使用 | 仅交易日轴与「事实值」；不得声称 PIT |
| 禁止行为 | 用 `effective_date` 冒充 `available_at`；用当前行业/市值/名称回填历史 |
