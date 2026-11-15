# M6.6 增补 Batch 6：统一 Gate 复检

- 日期：2026-11-16
- 依据：本轮 42 次 iFinD 真实请求 + M6.4/M6.5 既有证据
- 状态取值：`PASS / PARTIAL / FAIL / UNKNOWN / NOT_RUN`

---

## 1. G9（复权与公司行为）

| 子门禁 | 判定 | 证据 | 说明 |
| --- | --- | --- | --- |
| G9_PRICE_SEMANTICS | **PARTIAL** | 600519 前/后复权分别可取得；同问返回 52 行同列 | 无 `adjust_type` 字段，口径由措辞决定 |
| G9_ADJUSTMENT_FACTOR | **PARTIAL** | `raw×factor` vs 后复权最大偏差 1.8883bp；除权日 8.6760→8.8814 | 数值关系实测一致；缺官方方向/基准日定义 |
| G9_CORPORATE_ACTION | **FAIL** | 分红/送转字段全空；仅 300176 配股字段完整 | 无法覆盖现金分红与送转 |
| G9_EVENT_ACCOUNTING | **FAIL** | 无每股派现、无送转比例 | 无法构造现金/股份账本（仅配股一项） |
| G9_PIT | **FAIL** | 只有日期；分红日期为空；无公告时间 | 不能支撑 PIT |
| G9_PERSONAL_LICENSE | **PARTIAL** | 用户授权个人研究测试 | 无供应商书面许可 |
| G9_WEBSITE_LICENSE | **FAIL** | — | `WEBSITE_COMMERCIAL_LICENSE = NOT_ESTABLISHED` |
| **G9 总判定** | **FAIL** | — | 关键子项（CA/PIT）未通过 |

## 2. G3（历史板块成分）

| 子门禁 | 判定 | 证据 | 说明 |
| --- | --- | --- | --- |
| G3_HISTORICAL_SNAPSHOT | **FAIL** | 2025-03-05 与 2026-09-28 总数与前 100 行完全相同 | 日期被忽略，只返回当前成分 |
| G3_MEMBERSHIP_EVENTS | **FAIL** | 无 in/out 字段 | — |
| G3_DELISTED_COVERAGE | **UNKNOWN** | 当前快照无法验证 | — |
| G3_TAXONOMY_MAPPING | **UNKNOWN** | 只有板块名称 | 无法生成 provider_sector_id |
| G3_REPEATABILITY | **PARTIAL** | 两次不同日期返回一致 | 非严格同查询重复 |
| G3_PERSONAL_LICENSE | **PARTIAL** | 用户授权 | 无书面许可 |
| G3_WEBSITE_LICENSE | **FAIL** | — | 同上 |
| **G3 总判定** | **FAIL** | — | 历史语义不成立 |

## 3. G1 / G2（只读复核）

| 门禁 | 判定 | 说明 |
| --- | --- | --- |
| G1 指数对齐 | **PASS（仅 staging）** | 132 行保全；16/16 文件哈希未变；未合并研究库 |
| G2 板块日线 | **PARTIAL** | `sector_data` 返回当前成分与区间统计，不是标准日 OHLCV |

## 4. 模型与许可

| 项 | 状态 |
| --- | --- |
| RPS v3 / breadth v2 | `NOT_CREATED`（G9 未过） |
| Model A / B / A+B | `FROZEN` |
| 网站商业许可 | `NOT_ESTABLISHED` |
| 第三方供应商（Tushare/理杏仁/CnOpenData/Choice/JoinQuant） | 全部 `NOT_AUTHORIZED`，本轮 0 次调用 |

## 5. 与 M6.5 相比的变化

| 项 | M6.5 | M6.6（增补后） |
| --- | --- | --- |
| iFinD 真实调用 | 0（本轮新增 42） | 42 次，全部在 100 次硬上限内 |
| G9 价格因子语义 | UNKNOWN | **PARTIAL**（数值关系已验证 ≤1.89bp） |
| G9 公司行为 | FAIL | **FAIL**（字段存在但值为空；配股例外成功） |
| G3 历史成分 | FAIL | **FAIL**（已证实只返回当前成分） |
| 300750 定性 | 待裁决 | **UNRESOLVED**（iFinD 无足够字段） |
| 四个原始库 | 未变 | 未变 |
| M6.4 staging | 14/14 | 16/16（含 2 个边车文件） |

## 6. 结论

- 本轮取得的最实质进展是**价格侧**：累计因子与后复权序列在 2bp 内一致，且除权日连续性可解释。
- 公司行为与历史成分仍被供应商能力限制，**G9 与 G3 均不得 PASS**。
- 网站商业许可仍完全未建立。
