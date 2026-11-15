# M6.2 Batch 5：独立复现与确定性测试

- 脚本：`scripts/m62_reproduce.py`
- 产物：`reproduction_manifest.json`、`reproduction_diff.csv`
- 结论：**相同输入下两次运行的样本、Bootstrap、交易模拟、账本哈希全部一致（deterministic = true）；但确定性不等于正确性，价格口径与 PIT 缺陷仍未消除。**

---

## 1. 实验身份

| 项 | 值 |
| --- | --- |
| 数据库 | `backend/data/b1_research.db`，SHA256 `A93BC080EB9E366D08025C0A247E9AF525674A0294EC4D6012B7DBFD8941C56B` |
| 特征缓存 | `backend/data/m61_cache/attribution.json`，SHA256 `58AFF611…23AB` |
| M6.1 结果 | `reports/trend_engine/m61/experiment_results.json`，SHA256 `8C9193A3…03FD` |
| 交易模拟源码 | `backend/app/services/trend_engine/trade_sim.py`，SHA256 `0DBDFA9E…E0F8` |
| 复现脚本 | `scripts/m62_reproduce.py`，SHA256 `45EBC6F4…F70D` |
| Python / SQLite | 3.13.13 / 3.50.4 |
| 变体 / 参数 | A2；20 日；`next_open`；`cost_bps=15`；Bootstrap 500 次、seed=42 |
| 切片 | train 2025-10-14~2026-05-19；validation 05-20~07-27；test 07-28~10-09 |
| 测试集信号 | 60 条（样本 33 条 / 3 个信号日） |

`reproduction_manifest.json` 同时记录环境版本与脚本 SHA，满足「每份结果写入数据版本、配置 SHA、代码 SHA」。

---

## 2. 确定性对比

两次运行同一流程，比较 SHA256 指纹：

| 项目 | 两次一致 |
| --- | --- |
| A2 测试集 Bootstrap（`bootstrap_a2_test`） | **是** |
| 测试集样本指纹（日期/代码/超额排序后） | **是** |
| 交易模拟权益曲线指纹 | **是** |
| 逐日账本指纹（现金/市值/权益） | **是** |
| 逐笔交易指纹（代码/进出日/股数/盈亏/原因） | **是** |
| breadth SQL vs 纯 Python（2026-09-24） | **是** |
| RPS `ret20` 独立重算（2026-09-30） | 是（相关 0.9951，中位差 0.00275pp） |

`deterministic: true`；无浮点漂移、无字典序依赖、无时间依赖。

### 2.1 随机种子的敏感性

| 种子 | 有效日 | 均值 | 95% CI |
| --- | ---: | ---: | --- |
| 42（预注册） | 3 | 0.554 | `[0.332, 0.67]` |
| 7 | 3 | 0.550 | `[0.332, 0.662]` |

- 均值与下界稳定，上界变化 0.008pp；
- **但这是 3 个簇的伪稳定**：区间宽度由同一簇内 20 只股票主导，不能当作统计显著性；
- 该结果与 `01-m61-evidence-reconciliation.md` 的 `INSUFFICIENT_INDEPENDENT_DAYS` 判定一致。

---

## 3. 独立实现的交叉核验

| 指标 | 主实现 | 独立实现 | 结果 |
| --- | --- | --- | --- |
| breadth（涨跌平 / MA 上方 / 新高新低） | SQL 窗口函数 | 纯 Python 逐股滚动 | 3 个抽样日**逐项相等** |
| RPS `ret20` | 库内供应商/计算值 | 用 `stock_daily.close` 重算 | 相关 0.9951，中位差 0.00275pp |
| 交易账本 | 模拟器逐笔/逐日 | 独立恒等式检查（现金 + 市值 = 权益） | 误差 0.00 |
| M6.1 六组交易结果 | 报告 | 重跑模拟 | 收益/回撤逐位一致 |

---

## 4. JSON schema 校验

| 文件 | 状态 | 缺失字段 |
| --- | --- | --- |
| `bootstrap_reproduction.json` | OK | 无 |
| `walk_forward_reproduction.json` | OK | 无 |
| `trade_sim_audit.json` | OK | 无 |
| `market_data_audit.json` | OK | 无 |
| `history_independence.json` | OK | 无 |

校验为「必填键 + 嵌套子键」级别（`m62_reproduce.SCHEMA_RULES`），不是完整 JSON Schema；字段类型/取值域未覆盖，属于已知不足。

---

## 5. 边界与异常样本

| 场景 | 测试 | 期望行为 | 结果 |
| --- | --- | --- | --- |
| 空样本 Bootstrap | `test_bootstrap_returns_unknown_for_empty_sample` | `days=0`、`mean=None`、`UNKNOWN` | 通过 |
| 空样本日期聚类 | `test_describe_days_handles_empty_sample` | 全零、无异常 | 通过 |
| 空 breadth | `test_breadth_aggregate_handles_empty_rows` | 全零、不抛异常 | 通过 |
| 异常涨跌幅（-100%） | `test_prev_close_rejects_invalid_pct` | 无法反推前收 → 不做涨跌停判断 | 通过 |
| 同日重复信号 | `test_describe_days_clusters_by_signal_date` | 计入同一日期簇，重复对数可统计 | 通过 |
| 只有 1 个交易日 | `bt._bootstrap` 分支 | 返回 `UNKNOWN: 有效日期不足` | 通过 |
| 极小样本 | A2 测试集 33 条 / 3 日 | 如实输出，不修正 | 通过 |

---

## 6. 缓存与复现的已知问题

| 问题 | 现状 | 要求 | 处理 |
| --- | --- | --- | --- |
| 缓存键不含实验身份 | 只有 `backend/data/m61_cache/attribution.json` 一个文件 | 键必须含 DB SHA + 代码 SHA + 参数 | **未改**（避免覆盖历史缓存）；本轮只读；未来新缓存按身份命名 |
| M6 全样本未保存逐样本 | 只有聚合 JSON | 需可重算 | `UNVERIFIABLE`，已在 Batch 1 记录 |
| history.db 复现无法重跑 | 依赖同一缓存，且参数不同 | 需完整身份 | 未重跑；引用 Batch 1 结论 |
| 报告仍可由人工编辑 | 无生成校验 | 报告必须由脚本生成 | 本轮报告由脚本产出数字，但 Markdown 仍手工组织；未做到全自动 |

---

## 7. 结论

| 项 | 结果 |
| --- | --- |
| 相同输入 → 相同输出 | **通过** |
| 关键指标独立实现交叉核验 | **通过**（breadth / RPS / 账本） |
| JSON 必填字段校验 | **通过** |
| 异常/空/极小样本处理 | **通过**（返回 UNKNOWN/INVALID，不伪造数值） |
| 全部报告由脚本自动生成 | **未完全达到**（统计数字来自脚本，排版仍手工） |
| 缓存身份 | **未达到**（单文件缓存，无版本键） |

按 Batch 5 门禁：确定性成立，可以发布「复现结论」；但由于 Batch 4 的价格口径问题与 Batch 2 的 PIT 缺口，**仍不得发布任何新的模型有效性结论**。
