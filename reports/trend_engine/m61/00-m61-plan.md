# M6.1 研究计划与基线审计（Batch 0）

- 日期：2026-11-15
- 分支：`feat/trend-engine-m61`（从 M6.1 授权基线切出）
- 数据访问：全部 `mode=ro` + `PRAGMA query_only=ON`
- 生产：未连接、未部署、未改调度、未安装 `data_revision`

## 1. Git 基线核对

| 项 | 值 |
| --- | --- |
| 起点 HEAD | `32223ea`（= M6 `dbf7dd2` + 验收文档一个提交） |
| 远端 `origin/feat/trend-engine-v1` | `32223ea`（与本地一致） |
| M0～M6 提交 | `1651cf2` / `a5462ad` / `a18f0a1` / `a6a5295` / `469ede9` / `8dfe433` / `dbf7dd2` |
| 工作区 | 仅 `reports/s6/s62/migration-report.json`（既有、非本批次产生、未触碰） |
| 新分支 | `feat/trend-engine-m61` |

**说明**：任务书写的 M6 基线是 `dbf7dd2`；实际 HEAD 多一个验收文档提交 `32223ea`。
两者代码完全一致（后者只新增 `M1-M6-ACCEPTANCE.md`），不构成基线冲突，已如实记录。

## 2. 数据库身份

| 库 | 字节数 | SHA256 |
| --- | ---: | --- |
| `backend/data/b1_research.db` | 1,441,378,304 | `A93BC080EB9E366D08025C0A247E9AF525674A0294EC4D6012B7DBFD8941C56B` |
| `backend/data/history.db` | 869,801,984 | `9267652175594871B49456253BB0437CD5738F0D19257AB8CCA961BA5F921A6A` |
| `backend/data/b2_final_snapshot.db` | 1,441,378,304 | `371512BC81F00A604788B02AF7D4BE9467C75F28CDFB984F0187DD81001520D3` |
| `backend/data/fupan.db` | 513,519,616 | `B65D59A65AAEE286F65B91AEE70DF75FF9B34E83E6C266E69BE4C491996337EE` |

- 本批次只用 `b1_research.db`（主样本）与 `history.db`（长历史对照）。
- `fupan.db` / `b2_final_snapshot.db` 不参与本轮统计，只作身份记录。

## 3. 批次计划与门禁

| 批次 | 内容 | 门禁 |
| --- | --- | --- |
| 0 | 基线、数据库身份、计划 | Git/工作区/DB 身份一致（本文件） |
| 1 | 数据质量审计 | 四份审计产物完成；PIT 缺口明确 |
| 2 | A/B 失效归因 | 分布、MFE/MAE、分层、因子 IC、消融、最差/最好特征差异全部产出 |
| 3 | 双模型重设计 | 先写设计文档、预注册少量变体，再跑训练/验证；不得看测试集调参 |
| 4 | Purged WFA + Embargo + Bootstrap | 至少 3 个非重叠窗口；日期聚类；多重检验校正 |
| 5 | 交易级风险退出模拟 | 下一日成交、涨跌停/停牌、滑点/佣金/印花税、仓位与持仓上限 |
| 6 | 组合研究 + 总验收 | A/B 独立通过才组合；否则如实结论为未通过 |

## 4. 预先声明的准入标准（不得事后降低）

1. 样本外 20 日超额 > 0；
2. 日期聚类 bootstrap 95% CI 下界 > 0；
3. 有效独立观察 ≥ 200（按**独立交易日**聚类后仍满足）；
4. 至少 3 个非重叠 walk-forward 窗口方向一致；
5. 前 5% 样本收益贡献 ≤ 50%，剔除后均值 > 0；
6. 不能只在单一市场环境有效（至少 2 个环境不为负）。

任一条不满足 → 结论为「未通过」，不得进入 M7。

## 5. 数据安全与停止条件

- 只读连接；新增实验只写 `backend/data/m61_cache/`（gitignore）与 `reports/trend_engine/m61/`；
- 不创建/覆盖/删除任何数据库；不安装 `data_revision`；不连生产；
- 出现以下任一情况立即停止并报告：PIT 无法证明、只读约束失效、发现未来数据泄漏、
  研究库与生产库混淆、样本不足、测试不通过、关键数据缺失导致统计不可靠。

## 6. 本批次新增代码

| 文件 | 用途 |
| --- | --- |
| `scripts/audit_trend_data_m61.py` | 数据质量审计 + 宽度重算（只读） |
| `scripts/analyze_trend_models_m61.py` | A/B 失效归因与因子消融 |
| 后续批次 | purged WFA、交易级模拟、组合研究 |

## 7. 交付映射

| 任务书要求 | 本目录文件 |
| --- | --- |
| 计划 | `00-m61-plan.md`（本文件） |
| 数据质量 | `01-data-quality-audit.md`、`data_coverage_matrix.csv`、`data_gap_report.md`、`pit_integrity_report.md` |
| 失效归因 | `02-model-failure-attribution.md` |
| Model A 重设计 | `03-model-a-redesign.md` |
| Model B 重设计 | `04-model-b-redesign.md` |
| Walk-forward | `05-walk-forward-validation.md` |
| Bootstrap | `06-bootstrap-analysis.md` |
| 风险退出 | `07-risk-exit-backtest.md` |
| 组合 | `08-portfolio-backtest.md` |
| 总验收 | `09-m61-final-acceptance.md` |
