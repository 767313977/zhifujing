# S6 数据质量风险登记

| 风险 | 状态 | 影响 | 灰度措施 |
| --- | --- | --- | --- |
| 7 条 `NOT_COMPARABLE` | 保留（未关闭） | 情绪次日相关一致性检查 | 相关记录隔离，不进入 C0/Top20/交易信号；页面显示「不可比」 |
| 竞价真实数据未验收 | `REAL_AUCTION_DATA_NOT_VERIFIED` | 竞价异动、开盘承接 | 页面标注未验收；无 0926 不生成涨幅；无 0931 不生成承接变化；`PENDING` 不写 0 |
| 题材真实覆盖未验收 | `THEME_REAL_DATA_ACCEPTANCE_PENDING` | 题材交集 | 仅作为带质量标签的研究工具；不自动加入 C0 |
| 历史数据缺口 | `HISTORICAL_DATA_GAP_UNDER_REVIEW` | 质量台/工作台/漏斗 | 保留缺口状态；PIT 规则不变；不用 0 填充 |
| 空交易日历的接口行为 | 部分修复 | Staging 空库下 workbench 返回 400 | 生产有交易日历；Staging 显示准确错误，不伪装成功 |
| 质量状态被误显示 | 已核对 | 前端把 null 显示成 0 | 契约与测试覆盖；本轮未发现回归 |

## 关键数字（v4 真实库只读对账）

- `total_checks=76`、`passed=65`、`explained=4`、`not_comparable=7`、`unexpected=0`
- `exit_code=2`（fail closed）

## 结论

数据质量门禁**未被篡改**：7 条保留、没有新增 unexpected、没有把缺失改成 0。
灰度期间必须继续显示状态标签与 `as_of`，且质量失败的数据不得进入策略排序。
