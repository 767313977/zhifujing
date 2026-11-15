# M6.6 Batch 2：供应商许可与逐数据集审批闭环

- 日期：2026-11-16
- 台账：`provider_approval_ledger.csv`（18 行 × 19 列）
- 测试：`tests/test_trend_m66_ledger.py`（13 项通过）
- 网络调用：**0 次**；无注册、无购买、无 Token

---

## 1. 审查顺序与结果

严格按任务书顺序：Tushare Pro → 理杏仁 → CnOpenData → Choice → JoinQuant → iFinD（既有范围澄清）。

| 供应商 | 数据集行数 | 目标 Gate | 当前状态 |
| --- | ---: | --- | --- |
| Tushare Pro | 5 | G9-PRICE / G9-CA / G3 | 全部 `NOT_AUTHORIZED` |
| 理杏仁 | 3 | G9-CA | 全部 `NOT_AUTHORIZED` |
| CnOpenData | 2 | G9-PRICE / G9-CA | 全部 `NOT_AUTHORIZED` |
| Choice | 2 | G3 | 全部 `NOT_AUTHORIZED` |
| JoinQuant | 1 | G3 | `NOT_AUTHORIZED` |
| iFinD | 5 | G1 / G2 / G9 | 4 行 `AUTHORIZED_LIMITED`（仅 M6.4 范围）+ 1 行 `REJECTED`（无成分接口） |

状态分布：`NOT_AUTHORIZED` 13、`AUTHORIZED_LIMITED` 4、`REJECTED` 1。

## 2. iFinD 的 4 行 `AUTHORIZED_LIMITED` 是什么

它们是 **M6.4 已经执行过、并在 M6.4 验收中确认的最小范围**，不是 M6.6 新授权：

| 数据集 | 范围 | 依据 |
| --- | --- | --- |
| `get_stock_performance` | 600519 单券、除权日前后窗口 | M6.4 Batch 1/2 |
| `get_stock_events` | 600519 / 002594 / 300750 三券事件 | M6.4 Batch 2 |
| `index_data` | 6 指数 × 2026-09-18~10-09 | M6.4 Batch 3 |
| `sector_data` | 2 个板块样本 | M6.4 Batch 4 |

约束：**不得据此扩大调用范围**；M6.6 不新增 iFinD 请求。第 5 行（板块历史成分）为 `REJECTED`，原因是账号工具集中不存在该接口，不是许可问题。

## 3. 特别许可规则的落地

| 规则 | 台账中的体现 |
| --- | --- |
| Tushare 个人非商业许可不得默认扩展到网站 | 5 行 `website_internal_use_allowed=NOT_ALLOWED`、`public_display_allowed=NOT_ALLOWED`、`redistribution_allowed=NOT_ALLOWED` |
| 理杏仁内部使用许可不得扩展到对外网站 | 3 行同上；`derived_metrics_allowed=NOT_ALLOWED` |
| Choice 复制/镜像/衍生需书面许可 | 2 行 `written_license_status=NOT_OBTAINED`，`public_display=NOT_ALLOWED` |
| CnOpenData 商业版须核对合同具体数据集/频率/API/存储/展示 | 2 行 `pricing=需询价`、`retention_period=UNKNOWN`，未采购前不视为可商用 |
| JoinQuant 接口可用 ≠ 可导出/可商用 | 1 行 `local_storage_allowed=UNKNOWN`、`redistribution=NOT_ALLOWED` |
| 销售口头报价不作授权凭据 | 台账新增/变更只认 `written_license_status` 与 `evidence_uri`，口头内容只能进 `provider_reply_register.csv` 且不得改变 `approval_status` |

## 4. 测试强制的硬约束

| 测试 | 约束 |
| --- | --- |
| `test_no_third_party_authorized` | 除 iFinD 外任何供应商不得出现 `AUTHORIZED_*` |
| `test_authorized_rows_require_evidence` | `AUTHORIZED_*` 必须有 `evidence_uri` |
| `test_full_authorization_requires_approver_and_date` | `AUTHORIZED_FULL` 必须有批准人与批准日期 |
| `test_public_display_not_granted_in_this_stage` | 本阶段任何数据集都不得出现 `public_display_allowed=ALLOWED` |
| `test_third_party_storage_not_yet_allowed` | 五家第三方 `local_storage_allowed` 不得为 `ALLOWED` |
| `test_status_enum_only` | 状态只能取七种枚举值 |

这些测试的意义：**将来若有人把「注册成功」或「Token 可用」直接写成 `AUTHORIZED_FULL` 而没有证据，测试会失败。**

## 5. 仍未明确的项（全部标 UNKNOWN，不猜）

| 项 | 涉及供应商 |
| --- | --- |
| API 价格与配额 | 理杏仁、Choice、JoinQuant |
| 6000 积分实际门槛与费用 | Tushare `dc_member` |
| 保留期限（retention） | 全部 |
| 衍生指标许可 | Tushare、CnOpenData |
| 网站内部使用许可 | CnOpenData、理杏仁 |
| 更新频率是否满足日常研究 | CnOpenData（年度更新） |

## 6. Batch 2 验收

| 门禁 | 结果 |
| --- | --- |
| 每家供应商每个数据集独立记录 | 通过（18 行，覆盖 6 家） |
| 许可范围与使用场景逐项匹配 | 通过（19 列逐项填写） |
| 费用/配额/更新/历史范围明确或 UNKNOWN | 通过 |
| 未获书面授权的数据集仍为 `NOT_AUTHORIZED` | 通过（13 行） |
| 不因注册/Token/试用可登录自动放行 | 通过（测试强制） |
