# M6.5 Batch 3：G9 最小样本跨供应商交叉验证方案

- 日期：2026-11-15
- 性质：**验证方案**。本批次**未执行任何供应商取数**，10 个样本均为计划状态。
- 前置条件：任何第三方真实调用必须先在 Batch 7 审批表中获得用户逐项批准（当前全部 `NOT_AUTHORIZED`）。
- 输出：本文件、`g9_sample_manifest.csv`（10 个事件）、`g9_field_crosswalk.csv`（25 个规范字段 → 4 家供应商字段映射）

---

## 1. 目标

在**不改动模型**的前提下，回答三个问题：

1. 是否存在至少两家供应商，能对同一证券同一事件给出一致、可解释的复权价格？
2. 公司行为的**现金与股份变动**能否构造账户事件（金额 + 股数），而不是只有价格调整？
3. PIT 时间语义能否满足「只用当时已知信息」的要求，还是必须显式降级为 date-level？

只有三个问题都得到肯定回答，G9 才可能 PASS；否则宁可保持 FAIL。

## 2. 样本设计（固定 10 个，不挑选有利样本）

| 编号 | 类型 | 证券 | 锚点日期 | 选取依据 | 状态 |
| --- | --- | --- | --- | --- | --- |
| G9-CD-01 | 现金分红 | 600519.SH 贵州茅台 | 除权 2026-06-26 | M6.4 已用锚点 | `PLANNED_ANCHOR` |
| G9-CD-02 | 现金分红（待重新定性） | 300750.SZ 宁德时代 | 除权 2026-04-22 | M6.4 锚点 | `PLANNED_ANCHOR_RECLASSIFY` |
| G9-CD-03 | 现金分红 | 688270.SH 臻镭科技 | 除权 2026-06-17 | 规则 R1 | `PLANNED_PENDING_VERIFICATION` |
| G9-BT-01 | 送股/转增 | 002594.SZ 比亚迪 | 除权 2026-07-31 | M6.4 已用锚点 | `PLANNED_ANCHOR` |
| G9-BT-02 | 纯转增（10 转 4） | 001225.SZ 和泰机电 | 除权 2026-09-23 | 规则 R2 | `PLANNED_PENDING_VERIFICATION` |
| G9-BT-03 | 纯转增（每股 0.49） | 688110.SH 东芯股份 | 待确认 | 规则 R2 | `PLANNED_PENDING_VERIFICATION` |
| G9-RI-01 | 配股（10 配 4） | 300176.SZ 鸿特科技 | 股权登记 2026-08-12 | 规则 R3 | `PLANNED_PENDING_VERIFICATION` |
| G9-RI-02 | 配股 | 待规则确定 | 待定 | 规则 R4 | `PLANNED_PENDING_RULE` |
| G9-CT-01 | 无行为对照 | 600519.SH 贵州茅台 | 2025-07-01~2025-07-20 | M6.4 已用对照窗口 | `PLANNED_ANCHOR` |
| G9-CT-02 | 无行为对照 | 000001.SZ 平安银行 | 2026-01-05~2026-01-30 | 规则 R5 | `PLANNED_PENDING_VERIFICATION` |

选取规则（写入 manifest，执行时按此机械执行）：

- **R1**：2026 年沪市实施分红的科创板公司中，按除权日升序取首个（不与锚点重复）。
- **R2**：2026 年「仅有资本公积转增、无现金分红」的纯转增样本，深市、沪市各取 1 个，按除权日升序。
- **R3**：2026 年已披露实施公告的配股，按股权登记日升序。
- **R4**：若当年配股不足 2 只，回退到 2025 年最后一只实施配股（按除权日倒序）。**不得**用其他事件冒充配股。
- **R5**：2026 年 1 月无任何公司行为的深市大市值样本，且必须由至少两家供应商的**事件表为空**来确认「无行为」，不能凭印象判定。

## 3. 已发现的实质冲突（必须在验证中裁决）

M6.4 把 300750.SZ 在 **2026-04-22** 的事件记录为「配股（配售登记日为空）」。但理杏仁公开文档的 `cn/company/dividend` 示例（请求 300750，区间 2025-11-13~2026-11-13）返回：

```
registerDate 2026-04-21, exDate 2026-04-22, dividend 4.779
registerDate 2026-04-21, exDate 2026-04-22, dividend 2.178
```

即同一除权日为**现金分红**（两行对应不同分配口径），不是配股。

推论：

1. iFinD `get_stock_events` 返回的「向老股东配售股权登记日」为空，很可能是**字段不适用**，而不是「配股登记日缺失」；M6.4 把它归类为配股是**误判**。
2. 该事件同时也是「同一事件多行」的典型样本：账本必须按 `code + ex_date + record_date` 聚合，否则会把 4.779 与 2.178 各记一次现金流。
3. 本方案把 G9-CD-02 标为 `PLANNED_ANCHOR_RECLASSIFY`，验证第一件事就是裁决这一定性。

## 4. 每个样本必须采集的字段

以 `g9_field_crosswalk.csv` 的 25 个规范字段为准，分四组：

| 组 | 字段 |
| --- | --- |
| 价格 | `raw_open/high/low/close`、`forward_adjusted_close`、`backward_adjusted_close` |
| 因子 | `single_event_factor`、`cumulative_factor` |
| 事件会计 | `cash_per_share_pretax/posttax`、`stock_dividend_ratio`、`transfer_ratio`、`rights_ratio`、`rights_price`、`rights_shares` |
| 时间与治理 | `announcement_date`、`announcement_timestamp`、`record_date`、`ex_date`、`pay_date`、`share_listing_date`、`plan_status`、`revision_flag`、`available_at`、`payload_hash` |

采集窗口：`ex_date - 30 交易日 ~ ex_date + 30 交易日`；无行为对照取固定 20 个交易日窗口。

## 5. 交叉验证规则（可执行检查）

| # | 规则 | 判定方式 |
| --- | --- | --- |
| C1 | 至少两家独立供应商覆盖同一证券同一事件 | 事件键 `code + ex_date` 存在于 ≥2 家 |
| C2 | 不要求因子数值相同，但事件原始字段必须一致 | 比较 `cash_per_share`、送转比例、登记/除权日；允许口径差异需写明来源定义 |
| C3 | 同一供应商前/后复权必须可由字段区分 | 检查是否存在 `adjust_type/price_type` 或分列字段；否则该项记 FAIL |
| C4 | 逐日比较 OHLC | 输出绝对误差与相对误差（bp），阈值先记录不设通过线 |
| C5 | 除权日前后连续性 | 用 `raw × 累计因子` 与供应商复权价在 `ex_date ± 5` 日逐日比对 |
| C6 | 现金流与股份变化不重复计入 | 事件账本按 `code + ex_date + record_date` 聚合，多行事件只生成一组现金流 |
| C7 | 公告日期不得被登记日/除权日代替 | 若 `announcement_date` 缺失，记 `PIT_DATE_LEVEL_MISSING`，不得填入 `record_date` |
| C8 | 日期级 vs 时间戳级 | 只有日期 → `pit_precision=date`；四家均无时分秒，默认全部 date-level |
| C9 | 历史成分/事件不得用当前快照反推 | G9 事件以供应商事件表为准，禁止用「当前复权因子倒推历史」 |
| C10 | 原始 payload 必须落盘并哈希 | 每个供应商原始 JSON 保存，计算 SHA256，写入 staging |

## 6. G9 判定规则

| 子项 | PASS 条件 | 当前预判 |
| --- | --- | --- |
| G9-P 价格子项 | 复权方向/基准日/累计规则书面明确；≥2 家逐日误差可解释；除权日前后连续 | 取决于 Tushare 是否书面澄清因子方向 |
| G9-A 会计子项 | 现金分红与送转各 ≥3 个可核验样本；配股单独验证；金额+股数可构造账本 | 理杏仁字段最全，但 `plan_status`/`revision` 缺失 |
| G9-T PIT 子项 | 公告时间可确认，或对依赖精确时间的特征显式禁用 | 四家均只有日期 → 只能 date-level |
| G9-R 修订子项 | 历史修订策略明确 | 四家均不完整 |
| **G9 总判定** | 以上全部满足才可 PASS | 预警：即使价格子项通过，**总 G9 仍可能 FAIL** |

明确约束：**若只能验证价格调整而无法构造现金/股份事件账本，G9 价格子项可单独 PASS，交易会计子项仍 FAIL，总 G9 不得判 PASS。**

## 7. 执行流程（获批后按序执行）

1. 用户在 Batch 7 审批表中批准具体供应商（Tushare / 理杏仁 / CnOpenData / Choice）。
2. 创建唯一 staging 批次：`backend/data/m65_staging/<batch_id>/`。
3. 对每个样本、每个已批准供应商：调用 → 保存原始 payload → 计算 `payload_hash` → 写入 `corporate_action_raw` / `adjustment_factor_raw`。
4. 运行字段映射（crosswalk），输出归一化视图，并记录其依据的源记录 ID。
5. 执行 C1~C10 检查，产出 `g9_cross_provider_result.csv`。
6. 按第 6 节判定，更新 Gate 矩阵。
7. 若供应商返回错误码或空表：记 `ingestion_error`，**不得**当作「无事件」。

## 8. 预算估算（仅估算，未发生）

| 供应商 | 预计调用 | 说明 |
| --- | ---: | --- |
| Tushare | ≈40 次 | 10 样本 × (`adj_factor` + `daily` 两窗口) + `dividend`；均在 200 次/分钟档内 |
| 理杏仁 | ≈30 次 | 10 样本 × (`dividend` + `allotment` + `equity-change`) |
| CnOpenData | 0 API | 文件交付，无调用概念 |
| Choice | ≈15 次 | 仅 em_sector 与必要行情；需套餐权限 |
| iFinD | 0~2 次 | 复用 M6.4 数据；仅 300750 定性复核可能新增 1~2 次 |

## 9. 交付物（本批次已产出 / 后续待产出）

- 已产出：`03-g9-cross-provider-validation-plan.md`、`g9_sample_manifest.csv`、`g9_field_crosswalk.csv`
- 获批后待产出：`g9_cross_provider_result.csv`、各供应商原始 payload、差异说明

## 10. 未授权声明

本批次未注册、未购买、未调用任何第三方接口，未使用任何 Token，未下载任何数据。样本中的日期与代码均来自**公开公告/公开文档**，仅用于方案设计，不构成数据可用性证明。
