# M6.7 G3 历史板块成分契约

- 实现：`data_contracts.SectorMembershipSnapshot` / `SectorProviderMapping`
- 公开证据：M6.5/M6.6 的供应商文档调查（零调用）
- 结论：**当前没有任何已授权供应商能提供「按历史日期还原成分集合」**

---

## 1. 必须区分的六类对象

| 对象 | 定义 | 本阶段状态 |
| --- | --- | --- |
| 当前成分快照 | 查询时点的成员列表 | iFinD `sector_data` 仅此 |
| 历史日期成分快照 | 指定历史日期当时的成员 | 无授权供应商 |
| 成分变更事件 | 加入/剔除及日期 | 无授权供应商 |
| 行业分类历史 | 证券行业归属随时间的变动 | Tushare `index_member_all` 有 in/out（未授权） |
| 概念板块历史 | 概念成员的历史截面 | Tushare `dc_member` 仅 2024-12-20 起（未授权） |
| 指数成分历史 | 指数样本历史 | Choice `em_sector(isHistory=1)`（未授权） |

**禁止**把「有当前成分接口」写成「支持历史成分」。

## 2. 字段契约

| 字段 | 类型 | 空值语义 | 说明 |
| --- | --- | --- | --- |
| `provider` | str | 必填 | 供应商 |
| `provider_sector_id` | str? | `NOT_PROVIDED` | 供应商板块代码；iFinD 只有名称 |
| `canonical_sector_id` | str? | `NOT_PROVIDED` | 映射到 b1 后的规范 ID |
| `taxonomy` | str | 必填 | 分类体系（IFIND / DC / SW2021 / CHOICE） |
| `sector_name` | str? | `NOT_PROVIDED` | 名称 |
| `security_id` | str | 必填 | 成分证券 |
| `snapshot_date` | str | 必填 | 截面日期 |
| `membership_status` | enum | — | `MEMBER / NOT_MEMBER / UNKNOWN` |
| `in_date` / `out_date` | str? | `NOT_PROVIDED` | 仅 `INTERVAL` 模式允许 |
| `announced_at` | str? | `NOT_PROVIDED` | 公告时间 |
| `available_at` | str? | `PIT_UNVERIFIED` | 可得时间 |
| `ingested_at` | str | 必填 | 采集时间 |
| `source_version` | str? | `NOT_PROVIDED` | 数据版本 |
| `is_current_snapshot` | bool | — | 当前快照标识 |
| `date_param_honored` | bool | — | 日期参数是否真的改变结果 |
| `coverage_scope` | str? | `NOT_PROVIDED` | 覆盖范围（沪深/北交所/退市） |
| `quality_status` | enum | — | 六态 |

## 3. 判定规则

1. `is_current_snapshot=True` → `CURRENT_SNAPSHOT_ONLY`，禁止进入历史回测；
2. `date_param_honored=False` → 不得视为历史（即使填了历史日期）；
3. `available_at > snapshot_date` → `AVAILABLE_AFTER_SNAPSHOT_DATE`；
4. `SNAPSHOT_ONLY` 模式禁止携带 in/out（复用 `multisource.assert_snapshot_mode`）；
5. 当前成分不得回填历史（`assert_snapshot_not_from_current`）。

## 4. M6.6 实测对照

| 观测 | 契约判定 |
| --- | --- |
| 芯片 941 条、电力设备 418 条 | 当前快照 |
| 2025-03-05 与 2026-09-28 总数与前 100 行一致 | `date_param_honored=False` |
| 无板块代码、无 in/out | `provider_sector_id=NOT_PROVIDED` |

## 5. 验收影响

G3 只有在「按历史日期还原集合 + 可复现 + 退市覆盖 + 映射审批 + PIT + 许可」全部满足时才可 PASS。当前全部缺项，判定 **FAIL**。
