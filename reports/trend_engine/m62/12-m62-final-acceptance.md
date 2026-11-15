# M6.2 总验收报告

- 日期：2026-11-15
- 分支：`feat/trend-engine-m62-data-integrity`（起点 `782a5f3`）
- 数据访问：全部 `mode=ro` + `PRAGMA query_only=ON`
- 生产：未连接、未部署、未改调度、未安装 `data_revision`、未合并 master
- **最终判定：`M62_PARTIALLY_ACCEPTED`**
- **Model A / B / A+B 仍为 M6.1 未通过；不申请 M7；不重启模型训练。**

---

## 1. 判定理由（一句话）

证据链、统计口径、PIT 契约、市场数据规范、交易账本与确定性复现均已完成并通过；但**交易级价格口径（未复权）与公司行为缺失属于 HIGH 级缺陷**，Batch 4 门禁未通过，且 PIT/退市/ST/板块成分数据仍不可用，因此只能「部分通过」。

---

## 2. 批次与提交

| 批次 | 内容 | 提交 |
| --- | --- | --- |
| 0 | 基线与安全、只读探针 | `a41104d` |
| 1 | M6.1 证据对账、Bootstrap/WF 复算、history 独立性 | `a2d06f4` |
| 2 | PIT 契约、证券主数据设计、数据源评估 | `b82f056` |
| 3 | 指数/板块/breadth/RPS 规范与双实现核对 | `15e1f15` |
| 4 | 交易模拟器审计、账本重建、最小修复 | `81c2a89` |
| 5 | 确定性复现、schema 校验、边界样本 | `255b4e6` |
| 6 | 预注册协议、研究重启闸门、本报告 | 见本节末尾 |

---

## 3. 交付物清单

| 文件 | 状态 |
| --- | --- |
| `00-baseline-and-safety.md` | 完成 |
| `01-m61-evidence-reconciliation.md` | 完成 |
| `02-pit-contract.md` | 完成 |
| `03-security-master-history-design.md` | 完成 |
| `04-data-source-assessment.md` | 完成 |
| `05-market-data-contract.md` | 完成 |
| `06-breadth-calculation-spec.md` | 完成 |
| `07-rps-versioning-and-quality.md` | 完成 |
| `08-trade-simulator-audit.md` | 完成 |
| `09-deterministic-reproduction.md` | 完成 |
| `10-next-research-preregistration.md` | 完成 |
| `11-research-restart-gates.md` | 完成 |
| `12-m62-final-acceptance.md` | 本文件 |
| CSV | `evidence_traceability.csv`、`data_field_lineage.csv`、`pit_capability_matrix.csv`、`market_data_gap_matrix.csv`、`sample_day_reconciliation.csv`、`trade_ledger_reconciliation.csv`、`portfolio_daily_reconciliation.csv`、`reproduction_diff.csv` |
| JSON | `readonly_probe.json`、`bootstrap_reproduction.json`、`walk_forward_reproduction.json`、`history_independence.json`、`data_catalog.json`、`pit_inventory.json`、`market_data_audit.json`、`trade_sim_audit.json`、`reproduction_manifest.json` |
| 代码 | `scripts/m62_*.py`（7 个脚本）、`trade_sim.py` 最小修复 |
| 测试 | `tests/test_trend_m62_readonly.py`、`tests/test_trend_m62_tools.py`、`tests/test_trend_trade_sim.py`（新增 3 项） |

---

## 4. 关键结论

### 4.1 M6.1 证据对账

| 疑点 | 结论 |
| --- | --- |
| A 的两套 Bootstrap CI | `09` 号报告的 `[-1.53, 0.30]` 来自 **M6 全样本**（217 日、1000 次）；M6.1 冻结测试是 `[0.332, 0.67]`（3 个信号日）。**引用错误，不是计算错误**，复算逐位一致。 |
| A 的 3 个有效日 | 33 个样本 = 3 天（5 / 8 / 20），无重复对；属于 `INSUFFICIENT_INDEPENDENT_DAYS`，不得称通过。 |
| WF 区块数 | 实际 **3 块**（`range(1,4)`），第 1 段为初始训练段不评估；embargo 只记录、未参与剔除；流程是「分块评估」而非逐块重训。 |
| A2 验证失败仍报测试 | 选参只用训练集；测试无条件执行一次且 `passed=False`；测试值只能作探索性诊断。 |
| history.db 独立性 | 独立数据库、独立区间，但证券池 100% 被 b1 覆盖、无退市股，且重新选参（A3/B0）→ **独立数据库复现**，不是独立 OOS。 |

### 4.2 数据治理

| 项 | 结论 |
| --- | --- |
| PIT 契约 | 10 字段已定义；核心 4 张表**无任何可见时间/版本字段** |
| 证券主数据 | 无上市/退市表、无 ST 公告、无历史行业/市值 |
| 指数/板块 | 区间内无缺口、无重复，但**滞后 5 个交易日** |
| 板块成分 | 仅 60 个交易日（23.5%），更早区间必须 `UNKNOWN` |
| breadth | 空表；规范与双实现已就绪（3 个抽样日逐项一致） |
| RPS | 251 天、无重复，但存在孤儿/混版本日 2026-10-21；`ret20` 重算相关 0.9951 |

### 4.3 交易级

| 项 | 结论 |
| --- | --- |
| M6.1 数字复现 | 6 组收益/回撤/交易数**逐位一致** |
| 账本恒等 | `cash + market_value == equity`，误差 0.00；已实现 + 未实现 = 权益变化，残差 ≤0.05 元 |
| 已修复 | 逐笔 `pnl` 未扣买入佣金（最小修复），新增逐日账本与期末持仓 |
| HIGH 缺陷 | 输入价格为**未复权**，与模块文档要求的前复权不符 → 除权日失真 |
| A+B `+0.34%` | 已实现盈亏 **-19,561**，期末未平仓浮盈 **+22,957**；正收益完全来自浮盈 + 少数交易，不是 alpha |
| B 组合回撤变差 | 趋势退出与「回调延续」节奏冲突，属策略性质，不是计算错误 |

### 4.4 确定性

- 两次运行：样本指纹、Bootstrap、权益曲线、逐日账本、逐笔交易哈希全部一致；
- breadth SQL vs Python、RPS 重算独立核验通过；
- JSON 必填字段校验 5/5 通过；
- 种子 42 vs 7：均值 0.554 vs 0.550，仍只有 3 个簇 → 伪稳定。

---

## 5. 最终验收标准逐条

| # | 标准 | 结果 |
| ---: | --- | --- |
| 1 | M6.1 关键统计数字可追溯对账 | **通过** |
| 2 | Bootstrap 与 WF 的实现、区块、簇数明确 | **通过** |
| 3 | A2 验证失败后测试结果的解释边界明确 | **通过** |
| 4 | PIT 字段契约完整 | **通过**（设计层面） |
| 5 | 数据缺口与数据源可行性有明确结论 | **通过** |
| 6 | breadth/RPS/指数/板块计算定义可复现 | **通过**（规范 + 双实现） |
| 7 | 交易模拟器关键不变量有测试 | **通过** |
| 8 | 逐笔交易账本与逐日资产账本一致 | **通过** |
| 9 | 结果复现具有确定性 | **通过** |
| 10 | 后端全量测试通过 | **通过**（见第 7 节） |
| 11 | Ruff 通过 | **通过** |
| 12 | 生产零触碰得到证明 | **通过** |
| 13 | 所有报告、代码、数据身份可追溯 | **部分通过**：统计数字可追溯；Markdown 排版仍手工；缓存无版本键 |

**13 项中 11 项通过、2 项部分通过**，且 Batch 4 的交易级门禁因价格口径未通过 → 总判定 `M62_PARTIALLY_ACCEPTED`。

---

## 6. 数据安全证明

| 检查 | 结果 |
| --- | --- |
| 只读探针（4 个库、7 条写语句） | `READONLY_PROVEN`；6 条硬拒绝，`DROP IF EXISTS` 为空操作 |
| 数据库 SHA256（批次前后） | `b1` `A93BC080…C56B`、`history` `926765…A6A`、`b2_final` `371512…20D3`、`fupan` `B65D59…37EE`，**均未变化** |
| 新建/覆盖/删除数据库 | 无 |
| `reports/s6/s62/migration-report.json` | 未触碰；diff 哈希 `6caebef8…` 与批次前一致 |
| 生产连接/部署/调度/依赖 | 未执行；未安装 `data_revision` |
| 历史提交 | 未 rebase / reset / force-push |

---

## 7. 测试与静态检查

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 新增单测 | `pytest tests/test_trend_m62_readonly.py tests/test_trend_m62_tools.py` | 17 passed |
| 模拟器回归 | `pytest tests/test_trend_trade_sim.py` | 12 passed |
| 全量后端 | `pytest -q` | 1864 passed, 4 skipped |
| Ruff | `ruff check scripts/m62_*.py backend/app/services/trend_engine/trade_sim.py tests/test_trend_*.py` | All checks passed |

全量结果：**1864 passed, 4 skipped**（479 秒）。相对 M6.1 的 1844 项：新增 M6.2 测试 17 项、交易模拟器新增 3 项，合计 +20。

---

## 8. 未解决问题

1. **价格口径**：未复权成交价 + 无公司行为表 → 交易级结果 `PENDING_REVIEW`；
2. **指数/板块滞后 5 个交易日**，最近 5 日只能 `UNKNOWN`；
3. **breadth 未落库**（本轮只给规范与离线结果，写库需授权）；
4. **RPS 混版本/孤儿日**未清理，只做隔离；
5. **ST / 退市 / 历史行业 / 历史市值**不可用；
6. **板块历史成分**只有 60 天；
7. **缓存身份**未版本化，单文件缓存仍可能被覆盖；
8. **M6 全样本**无法离线重算（未保存逐样本记录）；
9. 年化、暴露等指标在 240 个交易日样本上只能作参考。

---

## 9. 最终回答

| 问题 | 结论 |
| --- | --- |
| M6.2 是否通过 | **部分通过（`M62_PARTIALLY_ACCEPTED`）** |
| M6.1 数字是否可信 | 数值可复现；`09` 号报告的 CI 来源标注需按 `01` 号文件纠正 |
| 数据是否可 PIT | 否，核心表缺可见时间与版本 |
| 交易级结果是否可用 | 只能作近似；价格口径修复前 `PENDING_REVIEW` |
| Model A/B/A+B 状态 | **仍未通过，保持 M6.1 结论** |
| 是否申请 M7 | **否** |
| 是否重启模型研究 | **否**，须先通过 `11-research-restart-gates.md` 的 P0/P1 闸门 |
| 下一步 | 修价格口径 → 补指数/板块 → 重算 RPS/breadth → 接入 ST/退市/行业/市值 → 按预注册协议重新立项 |
