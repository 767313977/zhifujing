# M6.2 Batch 1：M6.1 证据对账与统计口径纠正

- 执行时间：2026-11-15
- 复现脚本：`scripts/m62_reconcile_m61_evidence.py`、`scripts/m62_history_independence.py`
- 输入：`backend/data/b1_research.db`（SHA256 `A93BC080…C56B`，只读）、`backend/data/m61_cache/attribution.json`（SHA256 `58AFF611…23AB`）
- 代码基线：`782a5f301bb9c7aeef62ed77408c8777c13593cc`；`scripts/run_m61_experiments.py` blob `a5780447e78c6b1464421dc07f1e222c7d6f012b`
- 证据文件：`bootstrap_reproduction.json`、`walk_forward_reproduction.json`、`evidence_traceability.csv`、`history_independence.json`
- **本报告不修改任何 M6.1 历史报告数字，只新增追溯与纠正说明。结论不变：A、B、A+B 仍未通过准入。**

---

## 1. 结论摘要

| 疑点 | 判定 | 一句话结论 |
| --- | --- | --- |
| A：两套 Bootstrap CI 不一致 | **已定案（非计算错误，是引用错误）** | `09-m61-final-acceptance.md` 的 A `[-1.53, 0.30]`、B `[-1.61, 0.14]` 是 **M6 全样本**（217/219 个信号日、1000 次迭代）；`06-bootstrap-analysis.md` 的 A `[0.332, 0.67]`、B `[-2.638, 0.508]` 是 **M6.1 冻结测试集**（3/27 个信号日、500 次迭代）。两者统计对象不同，被并列引用。 |
| B：WF 区块数 4 vs 3 | **已定案（文字错误）** | 实际只生成 **3 个测试块**：`purged_blocks(days, blocks=4)` 的循环是 `range(1, 4)`；第 1 段（约 20% 日期）被当作初始训练段，从未评估。embargo 只记录起点，**没有参与任何训练剔除**。 |
| C：A2 验证失败仍报测试 | **已定案（边界缺失）** | 选参只用训练集；测试集无条件被评估一次，只写入 `frozen_test` 并标记 `passed=False`，未回流到任何参数；但报告把 test 列与 train/validation 并列，缺少「描述性诊断」标注。 |
| D：history.db 独立性 | **已定案（概念误用）** | 独立数据库、独立日期区间，但证券池完全被 b1 覆盖（5108/5108），无退市股；且按 history 训练集**重新选参**（A3/B0），不是对 A2/B2 的样本外验证。 |

### 1.1 对 M6.1 结论的影响

- 两模型的准入判定**不变**：A 的 3 个有效日不足以支撑任何 CI；B 的 CI 跨 0；A2 验证为负；history 复现非 OOS。
- 需要撤回的是**表述**，不是数值：`09` 号报告第 4 节末行的 CI 来源标注错误；`05` 号报告「4 个连续测试块」的文字错误；`06` 号报告对 A 的「日均值 0.554 通过」需降级为 `INSUFFICIENT_INDEPENDENT_DAYS`。

---

## 2. 疑点 A：两套 Bootstrap CI

### 2.1 字段级追溯

| # | 报告位置 | 报告值 | JSON 路径 | 数据切片 | 脚本 → 函数 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `m61/09-m61-final-acceptance.md` §4 | A `[-1.53, 0.30]` | `m6/m6_summary.json#/models/A/full/bootstrap/20` = `{days:217, iterations:1000, mean:-0.644, ci_low:-1.53, ci_high:0.301}` | M6 全样本 2025-10-14~2026-10-09 | `scripts/backtest_trend_engine.py` → `summarize()` → `_bootstrap()` | 追溯一致；**对象是 M6，不是 M6.1** |
| 2 | 同上 | B `[-1.61, 0.14]` | `m6/m6_summary.json#/models/B/full/bootstrap/20` = `{days:219, iterations:1000, mean:-0.687, ci_low:-1.609, ci_high:0.14}` | 同上 | 同上 | 同上（`-1.609` 四舍五入为 `-1.61`） |
| 3 | `m61/06-bootstrap-analysis.md` | A 3 日 / `0.554` / `[0.332, 0.67]` | `m61/experiment_results.json#/models/A/frozen_test/bootstrap_20` | M6.1 冻结测试 2026-07-28~2026-10-09 | `scripts/run_m61_experiments.py` → `main()` → `bt._bootstrap()` | **独立复算一致** |
| 4 | 同上 | B 27 日 / `-0.981` / `[-2.638, 0.508]` | `m61/experiment_results.json#/models/B/frozen_test/bootstrap_20` | 同上 | 同上 | **独立复算一致** |

`m61/09` 的两个数字与 `stages/06-m6.md` §2.4（来源表：A 217 日 -0.64、B 219 日 -0.69）完全对应，可确认是**跨阶段误引**，而不是 M6.1 脚本产生了第二套结果。

### 2.2 复算证明

```
.venv\Scripts\python.exe scripts\m62_reconcile_m61_evidence.py
→ {"m61_bootstrap_reproduced": true, "wf_blocks": 3, "wf_match": {"A": [true,true,true], "B": [true,true,true]}}
```

| 模型 | 冻结变体 | 期望（M6.1 JSON） | 复算 | 一致 |
| --- | --- | --- | --- | --- |
| A | A2 | `{days:3, iter:500, mean:0.554, ci_low:0.332, ci_high:0.67}` | 完全相同 | 是 |
| B | B2 | `{days:27, iter:500, mean:-0.981, ci_low:-2.638, ci_high:0.508}` | 完全相同 | 是 |

复算条件与 M6.1 完全一致：`entry_mode=next_open`、`cost_bps=15`、horizon=20、`iterations=500`、`seed=42`、percentile 区间、按 `signal_date` 整簇重采样、结果保留 3 位小数。

### 2.3 A 的「3 个有效交易日」拆解

| 信号日 | 样本数 |
| --- | ---: |
| 2026-08-18 | 5 |
| 2026-08-31 | 8 |
| 2026-09-01 | **20（等于 TOP 上限）** |
| 合计 | 33 |

- 33 个样本全部落在 3 个交易日，无重复 `(signal_date, code)` 对（`duplicate_signal_pairs=0`）。
- 以交易日为重采样单位时，实际只有 **3 个簇**，其中一簇占 61% 样本，因此 `[0.332, 0.67]` 的窄区间是**簇内高度同质**造成的伪精度，不是统计证据。
- 该 CI 的正确标注应为 `INSUFFICIENT_INDEPENDENT_DAYS`（独立交易日 < 20），不得解释为「通过」。

B 的 27 个交易日、114 个样本无重复对；区间 `[-2.638, 0.508]` 跨 0，与 `passed=False` 一致。

### 2.4 统计口径唯一化

今后 M6.2 及以后所有引用必须写全：

| 项 | 唯一口径 |
| --- | --- |
| 重采样单位 | `signal_date`（同日多只股票为一个簇） |
| CI 方法 | percentile（排序后 2.5% / 97.5% 分位） |
| 迭代次数 / 种子 | 500 / 42（M6 为 1000 / 42；引用必须注明） |
| 精度 | 3 位小数 |
| 对象命名 | `M6-full` 与 `M61-frozen-test` 必须显式区分，禁止只写「Bootstrap CI」 |

---

## 3. 疑点 B：Purged Walk-forward 实际结构

### 3.1 代码事实

```
scripts/run_m61_experiments.py:239  def purged_blocks(days, blocks=4, embargo=20)
      edges = [int(size * i / blocks) for i in range(blocks + 1)]
      for index in range(1, blocks):        # 1,2,3 → 3 个测试块
```

- `range(1, 4)` 只产生 3 个块；第 0 段（`2025-10-14 ~ 2026-01-07`）从未被评估。
- `embargo_start = days[start-20]` 只是被写进结果；`test_days = days[start:end]`，**没有任何训练集参与**，所以 embargo 实际没有剔除任何样本。
- 因此该流程的准确名称是「**对单一冻结模型的 3 段分块评估**」，不是逐块重训的 walk-forward。

### 3.2 真实区块（复算与 M6.1 JSON 逐块一致）

| 模型 | 块 | 测试区间 | embargo 记录起点 | 交易日 | 20 日样本 | 20 日有效信号日 | 20 日超额 | 60 日超额 |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |
| A | 1 | 2026-01-08 ~ 2026-04-10 | 2025-12-09 | 60 | 577 | 49 | +2.80 | +2.95 |
| A | 2 | 2026-04-13 ~ 2026-07-09 | 2026-03-13 | 60 | 242 | 41 | +0.63 | +3.24 |
| A | 3 | 2026-07-10 ~ 2026-10-09 | 2026-06-11 | 60 | 34 | 4 | +0.51 | 无（数据不足） |
| B | 1 | 2026-01-08 ~ 2026-04-10 | 2025-12-09 | 60 | 807 | 52 | +2.11 | +3.61 |
| B | 2 | 2026-04-13 ~ 2026-07-09 | 2026-03-13 | 60 | 195 | 43 | +0.79 | +5.13 |
| B | 3 | 2026-07-10 ~ 2026-10-09 | 2026-06-11 | 60 | 115 | 28 | **-1.01** | 无（数据不足） |

复算一致性：`{"A": [true,true,true], "B": [true,true,true]}`（样本数、20 日超额、60 日超额逐块相同）。

### 3.3 标签跨越区块边界

| 模型/块 | 20 日标签越出测试块末端（样本 / 占比） | 60 日标签越出 |
| --- | --- | --- |
| A-1 | 50 / 577（8.7%） | 577 / 577 |
| A-2 | 20 / 242（8.3%） | 240 / 242 |
| A-3 | 0 | 0（无可计算样本） |
| B-1 | 52 / 807（6.4%） | 807 / 807 |
| B-2 | 8 / 195（4.1%） | 196 / 196（60 日样本 196） |
| B-3 | 0 | 0 |

- 20 日标签只有 4%~9% 越界，影响有限；
- **60 日标签在前两块几乎全部越界**，所以块 1、块 2 的 60 日超额不是「块内」结果，不能与 20 日结果同等解读；
- 第 3 块 60 日为空是因为数据库终点 2026-10-09，未来 60 个交易日无法观测（`samples=0`），不是 0 收益。

### 3.4 结论

| 说法 | 修正 |
| --- | --- |
| 「4 个连续测试块」 | **3 个**测试块（第 1 段为初始训练段，不评估） |
| 「每块前 20 个交易日为 embargo（不参与评估）」 | embargo 仅记录，**未参与剔除**；需改为「分块评估，无逐块重训」 |
| 「A 三块方向一致为正」 | 成立，但块 3 只有 **4 个有效信号日**，不能作为稳定性证据 |
| 「B 最后一块转负」 | 成立（-1.01，28 个有效日） |

---

## 4. 疑点 C：A2 验证未通过却展示测试结果

### 4.1 代码路径

| 步骤 | 代码 | 行为 |
| --- | --- | --- |
| 选参 | `run_m61_experiments.py:320-330` | `eligible` 仅由 **train** 的样本数与 20 日超额决定，`max()` 选训练最优；**不含 test 字段** |
| 验证判定 | `:338` | `passed_validation = validation.excess_mean > 0` |
| 测试执行 | `:342-346` | **无条件执行**，写 `frozen_test`；不因验证失败而跳过 |
| 准入 | `:350-355` | `passed = passed_validation and samples>=200 and excess>0 and ci_low>0` |

### 4.2 判定

- 测试集**没有**参与排序、选型、阈值选择或报告筛选；A2 的 `passed=False` 已如实记录。
- A2 的测试值 `+0.58`（33 样本 / 3 个信号日）只能标注为**探索性诊断**，不能作为 OOS 证据；`09` 号报告未加此边界，属于表述缺陷。
- B2 的测试 `-0.94` 同样只是描述性结果，不能用来说明「模型接近有效」。

### 4.3 报告层面的唯一修正

在引用测试结果处必须同时写出：`VALIDATION_FAILED → TEST_IS_DESCRIPTIVE_ONLY`。不得单独摘出 A2 的 `+0.58`。

---

## 5. 疑点 D：history.db 的独立性

证据：`history_independence.json`。

| 项 | b1_research.db | history.db |
| --- | --- | --- |
| 日期范围 | 2024-09-02 ~ 2026-10-09 | 2021-09-14 ~ 2026-10-19 |
| 交易日 | 506 | 1,230 |
| 证券数 | 5,303 | 5,108 |
| 表 | 个股/指数/板块/RPS/日历/情绪等 | **仅 `stock_daily` 单表** |
| 指数/板块/RPS/日历 | 有（部分滞后/空） | **无** |
| 30 日以上 stale 代码 | 0 | 0（无退市股） |

| 判定项 | 结果 |
| --- | --- |
| 独立数据库 | 是（不同文件、不同 SHA） |
| 独立证券池 | **否**：history 的 5,108 个代码 100% 包含于 b1 的 5,303 个代码，`history_only=0`；说明按**当前代码池**回补历史，退市股缺失 |
| 复用同一冻结变体 | **否**：history 训练集重新选参 → A3、B0（b1 为 A2、B2） |
| 纯净样本外 | **否**：同一批代码、同一数据供应商、日期区间重叠 2025-10~2026-10 |

history 结果复核：

| 模型 | 选中变体 | 验证超额 | 测试样本 | 测试有效日 | 测试 20 日超额 | Bootstrap CI |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| A | A3 | -7.34 | 43 | 15 | +0.54 | `[-1.819, 1.974]` |
| B | B0 | -8.88 | 419 | 28 | +0.99 | `[0.339, 1.899]` |

- B 的 CI 下界 >0，但**验证集 -8.88**，且 419 个样本只来自 28 个交易日（日均 15 只），按预注册规则仍判未通过；
- 该结果不能表述为「在独立数据上确认了 B 的正超额」，只能表述为「在另一数据库、另一组参数下出现一次正测试值，且验证失败」。

**概念修正**：M6.1 中的 history 部分应统一称为「**独立数据库复现（independent-database replication）**」，禁止称为「独立样本外验证（independent OOS validation）」。

---

## 6. 需要写入 M6.1 报告的纠正说明（不改数字）

以下三条作为纠错记录保留在本文件，历史报告原文不动：

1. `m61/09-m61-final-acceptance.md` §4 末行：`A [-1.53, 0.30]、B [-1.61, 0.14]` 的来源是 **M6 全样本**（1000 次迭代、217/219 个信号日），不是 M6.1 的冻结测试 Bootstrap。M6.1 的对应值为 A `[0.332, 0.67]`（3 个信号日 → `INSUFFICIENT_INDEPENDENT_DAYS`）、B `[-2.638, 0.508]`。
2. `m61/05-walk-forward-validation.md` 与 `run_m61_experiments.py:358/403` 的「4 个连续测试块」应为「**3 个测试块 + 1 个初始训练段**」；embargo 未参与剔除，该流程是分块评估而非逐块重训。
3. `m61/09` §4 中 A2「测试 +0.58」必须标注 `VALIDATION_FAILED → TEST_IS_DESCRIPTIVE_ONLY`，不得单独引用。

---

## 7. 未解决问题（UNVERIFIABLE 清单）

| 项 | 状态 | 原因 |
| --- | --- | --- |
| M6 全样本 CI 的离线重算 | `UNVERIFIABLE` | M6 只落盘聚合统计，未保存逐样本记录；只能做字段级追溯（报告值与 JSON 三位小数一致） |
| history.db 的 PIT 完整性 | `UNVERIFIABLE` | 单表、无首次入库时间、无退市股、无日历；无法证明当时可见性 |
| A 的「3 个信号日」是否因筛选条件过严 | 未判定 | 属于模型设计问题，M6.2 明确不调参、不重训 |
| embargo 未生效是否为设计意图 | 未判定 | 无设计文档说明；本轮只记录事实，不擅自改脚本 |

---

## 8. 复现命令与产物

```
.venv\Scripts\python.exe scripts\m62_reconcile_m61_evidence.py
.venv\Scripts\python.exe scripts\m62_history_independence.py
```

| 产物 | 内容 |
| --- | --- |
| `reports/trend_engine/m62/bootstrap_reproduction.json` | 两套 CI 的期望值/复算值、A 的逐日样本、统计口径、M6 追溯 |
| `reports/trend_engine/m62/walk_forward_reproduction.json` | 真实区块、逐块复算、标签越界 |
| `reports/trend_engine/m62/evidence_traceability.csv` | 关键数字的报告→JSON→脚本→函数→切片→DB SHA→Git SHA |
| `reports/trend_engine/m62/history_independence.json` | 证券池重叠、表结构、history 选参与结果 |

## 9. Batch 1 门禁

| 门禁 | 结果 |
| --- | --- |
| Bootstrap 两套 CI 已解释并复现 | 通过 |
| WF 实际区块、样本簇、embargo 行为明确 | 通过 |
| A2 验证失败后的测试结果边界明确 | 通过 |
| history 复现性质明确 | 通过 |
| 未修改历史报告数字、未静默覆盖证据 | 通过 |
