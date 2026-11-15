# M6.5 最终验收报告：多数据源资格审查与 G9/G3 接入准备

- 日期：2026-11-15
- 任务书：`Trend_Engine_M6.5_MultiSource_Data_Qualification_TRAE_v2.md`
- 起点分支 / HEAD：`feat/trend-engine-m64-ifind-data-ingest` / `a77e62154384e5d82331631cb0e6b4bc4d2446b4`
- 工作分支：`feat/trend-engine-m65-multisource-data`
- 阶段性质：数据源资格审查、最小样本验证方案、可授权接入准备。**不是模型研究，不是 M7。**
- 第三方调用：**0 次**。未注册、未购买、未使用任何 Token、未下载任何第三方数据。
- iFinD 调用：**0 次**（仅生成支持工单草稿，未发送）。
- 原始研究库：全程只读，SHA 未变。
- **最终状态：`M65_PROVIDER_READY_FOR_APPROVAL`**

---

## 1. 一句话结论

**多供应商架构与审批材料已就绪，但没有一家第三方获授权，因此 G9 与 G3 仍然 FAIL。** 本阶段的价值是把「再调一次 iFinD」变成「有据可依的供应商选择与审批清单」：Tushare 最可能补 G9 价格，理杏仁最可能补 G9 会计，Choice 最接近 G3，CnOpenData 是唯一明示可商用的离线备选。

## 2. 基线与分支

| 项 | 值 |
| --- | --- |
| 起点 HEAD | `a77e62154384e5d82331631cb0e6b4bc4d2446b4` |
| 起点远端 | 同名分支 `origin/feat/trend-engine-m64-ifind-data-ingest`，与本地一致 |
| M6.4 报告冻结 | 22 个文件逐文件 SHA256 记录于 `baseline_freeze.json` |
| M6.4 staging 冻结 | 14 个文件逐文件 SHA256；批次 `batch_20261115T152701Z` |
| M6.5 批次提交 | Batch 0~7 各自独立提交（见第 9 节） |

## 3. 候选供应商能力矩阵（摘要）

完整 26 行 × 17 列见 `02-multisource-capability-matrix.csv`。

| 供应商 | 数据集 | 关键结论 | 状态 |
| --- | --- | --- | --- |
| Tushare Pro | `adj_factor` / `daily` | 未复权日线 + 累计复权因子；盘前更新；2000 积分（200 元/年） | DOCUMENTED |
| Tushare Pro | `dividend` | 现金分红 + 送转 + 登记/除权/派息/实施公告日；**无配股** | DOCUMENTED |
| Tushare Pro | `dc_member` | 按交易日取概念/板块成分；**历史仅 2024-12-20 起**；6000 积分 | DOCUMENTED |
| Tushare Pro | `index_member_all` | 申万三级成分，含 `in_date/out_date/is_new`，示例含退市证券 | DOCUMENTED |
| 理杏仁 | `cn/company/dividend` | 公告日期 + 送股 + 转增 + 分红 + 登记/除权/到账日 | DOCUMENTED |
| 理杏仁 | `cn/company/allotment` | 配股比例/价格/数量 + 除权日 | DOCUMENTED |
| 理杏仁 | 复权因子 | 开放平台目录未见 | UNKNOWN |
| 理杏仁 | 行业成分 | 仅有分类元数据，无成分列表 | NOT_AVAILABLE |
| CnOpenData | 复权行情表 | 3 字段（代码/日期/复权因子），1991-12-19~2026-07-22，**年度更新** | DOCUMENTED |
| CnOpenData | 分红送股表 | 18 字段（税前/税后分红、送转比例、预案/实施公告日、是否变更过） | DOCUMENTED |
| Choice | `em_sector(isHistory=1)` | 历史成分**仅沪深股票、上交所期权**；其他板块只有最新 | DOCUMENTED |
| Choice | 终端「成份变动记录」 | 界面支持按区间查纳入/剔除；默认近一年 | DOCUMENTED |
| JoinQuant | `get_concept_stocks(date)` 等 | 公开 SDK 支持按日期取成分；导出与商用**未核对** | DOCUMENTED |
| iFinD | 复权因子 / 公司行为 / 成分 | 因子方向不明；无金额与公告时间；无成分接口 | SAMPLE_VERIFIED / NOT_AVAILABLE |

## 4. 证据分级

| 级别 | 内容 |
| --- | --- |
| 一手官方文档 | Tushare 文档站（`doc_id` 25/27/28/103/261/290/335/362/363/405）、理杏仁开放平台文档、CnOpenData 数据目录页、Choice 帮助中心与 EMQuantAPI 手册、Choice 最终用户许可协议、Tushare 数据服务协议、理杏仁用户协议 |
| 官方文档（未逐项核对） | Choice 公司行为指标、JoinQuant 许可与导出条款 |
| 二手线索 | 「Tushare `adj_factor` 是累计后复权因子」仅来自第三方解读，官方文档未明示方向 |
| 实测 | 仅 iFinD（M6.4 的 19 次调用、≈2,230 单元格）与 b1 只读查询 |

## 5. 授权状态（逐数据集）

| 数据集 | 目标 Gate | 当前授权 | 说明 |
| --- | --- | --- | --- |
| Tushare 复权因子/日线/分红/成分 | G9 / G3 | `NOT_AUTHORIZED` | 需账号与积分 |
| 理杏仁 分红/配股/股本变动 | G9 | `NOT_AUTHORIZED` | 需 Token |
| CnOpenData 复权/分红表 | G9 | `NOT_AUTHORIZED` | 需采购 |
| Choice 历史成分 | G3 | `NOT_AUTHORIZED` | 需终端 + 套餐 + 书面许可 |
| JoinQuant 成分 | G3（备选） | `NOT_AUTHORIZED` | 未核价 |
| iFinD 现有工具 | G1/G2 | 已授权（M6.4 范围） | 本阶段 0 次调用 |
| iFinD 支持工单 | G9/G3 | 草稿未发送 | 28 条问题待用户发送 |

## 6. 最小样本验证状态

| 方案 | 样本数 | 状态 |
| --- | ---: | --- |
| G9 跨供应商验证（`g9_sample_manifest.csv`） | 10 个事件 | **方案完成，0 个执行** |
| G9 字段 crosswalk（`g9_field_crosswalk.csv`） | 25 个规范字段 | 完成 |
| G3 历史成分验证（`g3_sample_manifest.csv`） | 5 板块 × 3 截面 = 15 | **方案完成，0 个执行** |
| G3 板块映射候选（`sector_id_crosswalk_candidates.csv`） | 25 条（5 板块 × 5 供应商） | 全部 `pending`，无一条获批 |

### 6.1 方案阶段的重要发现

1. **300750 事件定性冲突**：M6.4 记为「配股」，但理杏仁公开文档示例显示 2026-04-22 同日为现金分红（4.779 / 2.178 两行）。执行验证时第一件事就是裁决该事件性质。
2. **同一事件多行**：理杏仁对同一登记日/除权日可返回多行分红，账本必须按 `code + ex_date + record_date` 聚合，否则重复计现金流。
3. **公告时间普遍缺失**：四家候选都只能提供日期级公告时间，PIT 必须降级为 date-level。
4. **b1 板块口径**：`sector_daily` 去重 383 个板块，但 `sector_basic` 只有 374 个（270 `kph_selected` + 104 `kph_industry`）；b1 自有成分只覆盖 2026-07-03~2026-09-24（60 个交易日、275 个板块）。

## 7. Gate 状态

| Gate | M6.4 | M6.5 | 依据 |
| --- | --- | --- | --- |
| G1 指数对齐 | PASS（staging） | **PASS（仅 staging）** | 132 行保全；与 b1 重叠 30 行全部 ≤1bp；未合并 |
| G2 板块日线 | PARTIAL | **PARTIAL** | 仍只有 iFinD 区间统计；Tushare `dc_index` 需 6000 积分 |
| G3 历史成分 | FAIL | **FAIL** | 方案与映射候选就绪，0 家获授权 |
| G9 价格域/公司行为 | FAIL | **FAIL** | 方案就绪；iFinD 字段歧义未澄清；无新数据 |
| RPS v3 | NOT_CREATED | **NOT_CREATED** | G9 未过，禁止构造 adjusted |
| breadth v2 | NOT_CREATED | **NOT_CREATED** | ST 数据仍缺 |
| 模型 A/B/A+B | 冻结 | **冻结** | 未训练、未调参、未跑策略收益 |

> G1 staging PASS **不代表**正式研究库 G1 解锁。合并需重新单独授权。

## 8. staging 与原始库 SHA

| 对象 | SHA256 | 变化 |
| --- | --- | --- |
| `b1_research.db` | `A93BC080EB9E366D08025C0A247E9AF525674A0294EC4D6012B7DBFD8941C56B` | 未变 |
| `history.db` | `9267652175594871B49456253BB0437CD5738F0D19257AB8CCA961BA5F921A6A` | 未变 |
| `b2_final_snapshot.db` | `371512BC81F00A604788B02AF7D4BE9467C75F28CDFB984F0187DD81001520D3` | 未变 |
| `fupan.db` | `B65D59A65AAEE286F65B91AEE70DF75FF9B34E83E6C266E69BE4C491996337EE` | 未变 |
| M6.4 staging 14 个文件 | 见 `g1_staging_manifest.json` | 14/14 未变 |

本阶段**未创建任何新 staging 批次目录**（无第三方数据可落盘），`backend/data/m65_staging/` 保持为空。

## 9. 提交记录

| 批次 | 提交内容 |
| --- | --- |
| Batch 0 | 基线冻结、分支创建、`00-baseline-and-m64-freeze.md`、`baseline_freeze.json` |
| Batch 1 | iFinD 支持工单草稿 + 28 条问题矩阵 |
| Batch 2 | 多源能力矩阵（26 行）+ 逐供应商证据 |
| Batch 3 | G9 验证方案 + 样本清单 + 字段 crosswalk |
| Batch 4 | G3 样本方案 + 板块映射候选 |
| Batch 5 | 多供应商协议 + staging 护栏 + schema 草案 + 28 项测试 |
| Batch 6 | G1 staging 保全与只读复核 |
| Batch 7 | 审批矩阵 + 本报告 |

## 10. 测试与静态检查

| 项 | 结果 |
| --- | --- |
| 新增 M6.5 测试 | 28 项（provider 9 / staging 12 / guard 7） |
| 后端全量测试 | **1932 passed, 4 skipped**（386.76 秒，退出码 0） |
| Ruff（`backend` + `scripts` + `tests`） | All checks passed |
| mypy | `NOT_RUN`（环境未安装） |
| 原始库哈希回归 | 通过（`test_protected_databases_unchanged`） |

## 11. 尚未解决

1. **G9 复权字段定义**：iFinD 未书面澄清前/后复权方向与基准日；Tushare 官方文档也未明示 `adj_factor` 是前复权还是后复权因子（仅第三方解读称累计后复权）。
2. **G9 交易会计**：四家都缺「精确公告时间」；配股样本仅找到 1 只（300176 鸿特科技），第 2 只需按规则回退到 2025 年。
3. **G3 板块映射**：b1 的 `kph_*` 自建分类没有任何供应商官方对照表，25 条候选全部待人工审阅。
4. **G3 历史长度**：Tushare `dc_member` 仅 2024-12-20 起；Choice 历史成分限沪深股票/上交所期权。
5. **商用许可**：Tushare（个人非商业）、理杏仁（内部使用）、Choice（禁止复制/镜像）均与网站展示冲突；仅 CnOpenData 商业分析版明示可商用，但年度更新。
6. **G1 历史延伸**：2025-09-08 之前未验证；如需补数需先申请预算（估 ≈6 次调用、≈1.2 万单元格）。
7. **非交易日填充行**：iFinD 在假日返回 close 填充值（72 行），入库前必须按交易日历过滤。

## 12. 用户待决策事项（审批表）

逐项明细见 `m65_approval_matrix.csv`，全部 `NOT_AUTHORIZED`。

| 编号 | 事项 | 解决 Gate | 预计成本 | 是否需要新账号 |
| --- | --- | --- | --- | --- |
| A | Tushare Pro 试用/样本接口 | G9 价格 + G9 会计 + G3 部分 | 200 元/年（2000 积分）；`dc_member` 需 6000 积分 | 是 |
| B | 理杏仁 API 试用/样本接口 | G9 会计 | 未公开，需询价 | 是（Token） |
| C | CnOpenData 样本采购/试用 | G9（离线校验） | 需询价 | 是（销售签约） |
| D | Choice 历史成分试用/采购 | G3 | 需询价 | 是（终端） |
| E | JoinQuant 历史成分样本验证 | G3（备选） | 未核价 | 是 |
| F | iFinD 支持工单发送 | G9 + G2 + G3 | 0 元 | 否 |

每项已写明：接口/数据集、最小样本、预计调用或费用、许可用途、是否新增账号、本地存储位置、风险、批准后执行命令。

## 13. 最终状态

**`M65_PROVIDER_READY_FOR_APPROVAL`**

理由：

- 供应商架构、能力矩阵、G9/G3 验证方案、适配器与 staging 护栏、G1 保全均已完成并可复现；
- 但**没有任何第三方样本被验证**（0 次调用），因此不能是 `M65_PARTIALLY_ACCEPTED`；
- 也没有出现「全部不可用」的结论，因此不是 `M65_REJECTED`；
- 下一步取决于用户对审批表 A~F 的逐项决定。

**本阶段不解除模型研究冻结，不构成 M7 授权。**
