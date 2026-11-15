# M6.6 增补最终验收：iFinD 个人基础版真实样本测试

- 日期：2026-11-16
- 分支：`feat/trend-engine-m66-provider-validation`
- 起点 HEAD：`7b7889d`（M6.5 末次提交）
- 批次：`ifind_personal_100_20261115T162631Z`
- 预算：上限 100 次，**实际 42 次**（40 成功 / 2 失败）
- 原始 payload：未持久化（`RAW_PAYLOAD_PERSISTENCE_NOT_AUTHORIZED`）
- **最终状态：`M66_PARTIALLY_ACCEPTED`**

---

## 1. 一句话结论

**在 100 次预算内完成了真实样本测试：复权因子的数值关系已验证（≤1.89bp），配股字段完整可账；但现金分红与送转字段返回空值、板块成分只给当前快照，所以 G9 与 G3 仍然 FAIL。**

## 2. 分支与提交

| 项 | 值 |
| --- | --- |
| 起点 | `feat/trend-engine-m65-multisource-data` / `7b7889d` |
| 工作分支 | `feat/trend-engine-m66-provider-validation` |
| 远端 | 本轮结束前不 push（按主任务书：未获授权不推送） |

## 3. Batch 完成矩阵

| Batch | 内容 | 状态 |
| --- | --- | --- |
| 0 | 基线、并发、账号、哈希、staging、预算账本 | 完成 |
| 1 | 工具与字段能力盘点（6 次请求） | 完成 |
| 2 | G9 固定样本（30 次请求） | 完成（部分样本字段为空） |
| 3 | G3 固定样本（6 次请求） | 完成（证实仅当前成分） |
| 4 | 预算/错误/审计账本 | 完成（`04-request-budget-ledger.csv`） |
| 5 | 存储与许可约束 | 完成（SUMMARY_ONLY） |
| 6 | 验收判定 | 完成（`06-gate-recheck.md`） |
| 7 | 最终验收 | 本文件 |

## 4. 修改与新增文件

| 文件 | 说明 |
| --- | --- |
| `backend/app/services/trend_engine/budget_guard.py` | 预算守卫、用途闸门、凭据脱敏 |
| `scripts/m66_ifind_probe.py` | 真实样本探针（能力/G9/G3/指标） |
| `scripts/m66_export_ledger.py` | 导出预算账本与哈希复核 |
| `scripts/m66_g9_validator.py` | G9 语义验收器（离线纯函数） |
| `scripts/m66_freeze_baseline.py`、`scripts/m66_build_ifind_package.py`、`scripts/m66_ifind_reply_import.py` | 基线冻结、工单包、答复登记 |
| `tests/test_trend_m66_{budget,ledger,replies,g9}.py` | 41 项测试 |
| `reports/trend_engine/m66/*` | 主任务书 Batch 0~2 与工单包 |
| `reports/trend_engine/m66/ifind_personal_100/*` | 本轮 8 份交付 |

## 5. 供应商授权状态

| 供应商 | 状态 |
| --- | --- |
| iFinD（个人基础版） | `AUTHORIZED_LIMITED`：仅本轮 100 次个人研究测试 |
| Tushare / 理杏仁 / CnOpenData / Choice / JoinQuant | `NOT_AUTHORIZED`，本轮 0 次调用 |

## 6. iFinD 28 条问题答复状态

全部 `OPEN`（工单仍未发送）。本轮真实样本回答的是其中一部分**能力问题**，不能替代供应商书面答复：

| 问题 | 本轮实测补充 |
| --- | --- |
| Q-A1/A2（复权方向与字段） | 无 `adjust_type`；前/后复权靠措辞区分 |
| Q-A3/A4（因子定义） | 数值上与累计后复权一致，官方定义仍缺 |
| Q-B2（每股派现/送转字段） | 字段存在但样本值为空 |
| Q-B3（公告时间） | 只有日期 |
| Q-C1/C2（历史成分） | 不支持历史截面，只返回当前成分 |
| Q-D2/D4（许可） | 仍未确认 |

## 7. G9 十个样本逐项

| 样本 | 结果 |
| --- | --- |
| G9-CD-01 600519 | 价格 PARTIAL（因子 ≤1.89bp）；事件 FAIL（金额为空） |
| G9-CD-02 300750 | **UNRESOLVED_EVENT_CLASSIFICATION** |
| G9-CD-03 688270 | 事件 FAIL |
| G9-BT-01 002594 | 送转字段为空 → FAIL |
| G9-BT-02 001225 | 同上 |
| G9-BT-03 688110 | 同上 |
| G9-RI-01 300176 | **VERIFIED（部分）**：0.4 股/10 股、3.36 元、公告 20260810、除权 20260821、实际 1.4417 亿股 |
| G9-RI-02 | **NOT_RUN**（无候选，未盲目试参数） |
| G9-CT-01 600519 2025-07 | PARTIAL（因子恒 8.5308） |
| G9-CT-02 000001 2026-01 | PARTIAL（因子恒 154.3684） |

## 8. G3 十五个样本逐项

| 样本 | 结果 |
| --- | --- |
| 芯片概念 × 2025-03-05 / 2026-09-28 | 均返回 941 条当前成分，两次完全一致 → `CURRENT_SNAPSHOT_ONLY` |
| 电力设备 × 2025-03-05 / 2026-09-28 | 均返回 418 条当前成分，两次完全一致 |
| 其余 11 个截面 | **NOT_RUN**：已证实接口不支持历史日期，继续调用只会浪费预算 |

## 9. 哈希与 staging

| 项 | 结果 |
| --- | --- |
| 四个受保护库 SHA | **前后完全一致**（见 `sha_check.json`） |
| M6.4 staging | **16/16 未变**（14 证据文件 + 2 个 SQLite 边车） |
| 本轮 staging | `backend/data/m66_staging/ifind_personal_100_20261115T162631Z/`（不入 Git） |
| 原始响应 | **未保存**；只保存 `capability_summary.json`、`g9_*_summary.json`、`g9_metrics.json`、`request_budget_ledger.jsonl` 与哈希 |

## 10. 字段缺失、冲突与 PIT

- 现金分红/送转：字段 schema 存在、**值全空**（`FIELD_NOT_PROVIDED`）；
- 同一事件多行：本轮未出现可聚合的多行金额，但规则已实现（多行无方案标识 → `CONFLICT_MULTI_PLAN`，不求和）；
- 300750：事件分类 `UNRESOLVED`，不沿用 M6.4 的「配股」判断；
- PIT：只有日期级，无公告时间，不能升级为 timestamp；
- 复权：无字段级方向标识，`factor_type` 只能标 `UNKNOWN`（实测行为后复权）。

## 11. 网络调用、数据量与费用

| 项 | 值 |
| --- | --- |
| iFinD 请求总数 | **42**（能力 6 / G9 30 / G3 6） |
| 成功 / 失败 | 40 / 2（失败为我方 server 指向错误，非供应商问题） |
| 重试 | 0 |
| 返回行数（合计，按解析行） | ≈ 900 行（多数被服务端截断为前 100 行） |
| 额外费用 | **0 元**（未购买增量包、未升级套餐） |
| 第三方调用 | **0 次** |
| 额度余额 | 截图口径剩余 2,940，**实时未核验** |

## 12. 测试与检查

| 项 | 结果 |
| --- | --- |
| 新增 M6.6 测试 | 41 项通过（budget 13 / ledger 13 / replies 6 / g9 9） |
| M6.5 回归测试 | 28 项通过 |
| 后端全量测试 | **1973 passed, 4 skipped**（391.13 秒，退出码 0） |
| Ruff（`backend` + `scripts` + `tests`） | All checks passed |
| mypy | `NOT_RUN`（环境未安装） |
| 敏感信息扫描 | 账本/报告无 Token；Token 值从未读取打印 |

## 13. 尚未解决

1. 现金分红与送转的金额/比例在 iFinD 返回空值，原因未知（权限、问法或数据覆盖）→ 需供应商书面答复。
2. 无 `adjust_type` 字段，前/后复权只能靠措辞区分。
3. 300750 2026-04-22 事件分类未裁决。
4. 板块历史成分与 in/out 事件在 iFinD 不可用。
5. 网站商业许可完全未建立。
6. 账号额度只能靠截图，无法实时核验计量。

## 14. 用户待决策

| 事项 | 目的 |
| --- | --- |
| 发送 iFinD 工单（28 问） | 澄清分红字段为空、复权方向、历史成分 |
| 批准 Tushare 2000 积分试用 | 补齐现金分红/送转 + 累计因子交叉验证（约 200 元/年） |
| 批准理杏仁 API 试用 | 补齐分红送转与配股明细（价格需询价） |
| 批准 Choice 或 Tushare 6000 积分 | G3 历史成分（唯一可行路径） |
| 是否继续消耗剩余额度 | 本轮 42/100，剩余 58 次可做交叉复现或补充样本 |

## 15. 阶段状态

**`M66_PARTIALLY_ACCEPTED`**

理由：真实样本已执行，价格因子关系与配股字段获得可复现证据；但 G9-CA/PIT 与 G3 受供应商能力限制失败，网站许可未建立。不属于 `M66_ACCEPTED`，也不是 `M66_BLOCKED_PENDING_AUTHORIZATION`（因为确实完成了授权范围内的取数）。

## 16. 强制声明

- Model A、B、A+B **仍冻结**；未训练、未调参、未跑策略收益。
- **不申请 M7**，不启动新模型训练。
- **G1 staging PASS 不等于研究库解锁**；本轮未合并任何 staging。
- M6.6 结束**不自动授权**数据合并或网站展示。
- `WEBSITE_COMMERCIAL_LICENSE = NOT_ESTABLISHED`。
