# B0 最终交付报告

> 日期：2026-11-08
> 分支：`feat/b0-research-foundation`
> 基线：`44f0912d9f754e253b06e1296cb1f8b381f158a0`（master）
> 范围：隔离开发与测试；**未连接生产、未部署、未改生产库、未重启服务**

---

## 1. 阶段 A 第 14.2 节八项问题的决策与落实矩阵

| # | 阶段 A 问题 | 决策 | B0 落实 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | 是否把统一回测提前到 B0 | **批准** | `services/research_backtest.py` + `scripts/backtest_research.py`；三个方向已复测 | 已完成 |
| 2 | 是否批准 `sector_member` 60/120 日回补 | **有条件批准**：先试验，再 60 日；120 日暂不执行 | 隔离副本上完成 25 个板块试验；60 日未执行（见 §4） | 试验完成，60 日待批准 |
| 3 | 是否同意 `RpsDaily` 加 4 个可空列 | **批准** | 模型、迁移、回滚、回补脚本、测试全部完成 | 已完成 |
| 4 | 趋势池是否排除北交所 | **排除**（研究候选池层） | 信号提供器默认 `exclude_bse=True`；原始行情不删除；涨跌幅按 `limit_rules` 分板块 | 已完成 |
| 5 | 默认持有期与基准 | 5/10/20/40/60；全市场等权；可选指数 | 内核默认值与 CLI 已实现；指数基准可选 | 已完成 |
| 6 | 费用默认值 | 同时展示 0 bps 与 15 bps | CLI `--cost-bps`；0/15 两档结果已输出；公式只扣一次 | 已完成 |
| 7 | 状态命名 | 接受「新晋强势 / 持续走强 / 强势整理 / 强度回落 / 持续弱势 / 数据不足」 | 仅写入 P0-1 设计；B0 不实现页面状态 | 设计已定，未编码 |
| 8 | 低位启动状态是否落库 | **先按请求计算，不建表**；P0-3 再评估 | B0 未新增表；内核可复用于 P0-3 | 设计已定，未编码 |

---

## 2. Git 基线与修改文件

### 2.1 基线

- 阶段 A 报告提交：`1bcead1 docs(research): 阶段A盘后研究升级审查与设计报告`
- B0 分支：`feat/b0-research-foundation`
- **B0 改动当前在分支工作区，尚未提交**（等待审核后再决定 commit / 合并）；
- 未合并 master，未推送远程，未部署。

### 2.2 修改文件

| 文件 | 变更 |
| --- | --- |
| `backend/app/models.py` | `RpsDaily` 增加 `ret20/ret60/rps20/rps60` 四个可空列 |
| `backend/app/jobs/scan_rps.py` | 窗口扩为 20/50/60/120/250；新增 `pool_count`、`with_meta`、`estimate_pool_count` |
| `scripts/backfill_sector_members.py` | 重写完整性判定、manifest、断点续跑、隔离库保护、在线备份复制 |
| `tests/test_scan_rps.py` | 新增 20/60 扩展、覆盖率口径、幂等测试 |
| `scripts/audit_research_data.py` | 新增只读审计脚本 |
| `scripts/backfill_rps.py` | 新增 RPS 五档回补脚本 |
| `scripts/migrate_rps_columns.py` | 新增迁移 / 回滚 / 结构检查 |
| `scripts/backtest_research.py` | 新增统一回测 CLI |
| `backend/app/services/research_backtest.py` | 新增统一回测内核 |
| `tests/test_research_backtest.py` | 新增 13 项内核测试 |
| `tests/test_backfill_sector_members.py` | 新增 12 项回补状态测试 |
| `tests/test_migrate_rps_columns.py` | 新增迁移/回滚测试 |
| `docs/b0/*` | 审计、回补试验、RPS、内核、基线、最终报告 |

> 未提交 `backend/data/`（含 490MB 主库副本、trial 库、JSON 证据），避免把大文件与数据副本推进仓库。

### 2.3 关键函数

| 模块 | 关键函数 |
| --- | --- |
| `backend/app/models.py` | `RpsDaily`（新增 4 列） |
| `backend/app/jobs/scan_rps.py` | `scan(pool_count, with_meta)`、`estimate_pool_count` |
| `backend/app/services/research_backtest.py` | `load_matrix`、`run_signals`、`_sample_for`、`_find_entry`、`_find_exit`、`Benchmark`、`_aggregate`、`_regime_map`、`signals_from_rps`、`signals_from_trend_template`、`signals_from_pattern`、`signals_from_sector_stock` |
| `scripts/backfill_sector_members.py` | `main`、`_classify`、`_day_boards`、`_target_days`、`_upsert_manifest`、`_copy_db` |
| `scripts/backfill_rps.py` | `main`、`_target_days`、`_status` |
| `scripts/migrate_rps_columns.py` | `apply`、`rollback`、`_columns`、`_backup` |
| `scripts/audit_research_data.py` | `main`、`audit_stock_daily`、`audit_sector_daily`、`audit_sector_member`、`audit_rps`、`audit_sector_lifecycle` |
| `scripts/backtest_research.py` | `main`、`_date_range`、`_result_json` |

**本轮未新增 HTTP API**；B0 只交付数据地基、回测内核与 CLI。P0 阶段再把内核接到现有 `sector` / `pools` 接口的 `track` 端点。

---

## 3. 历史数据完整性审计（摘要）

完整报告：[2026-11-08-b0-data-audit.md](2026-11-08-b0-data-audit.md)

| 表 | 结论 |
| --- | --- |
| `stock_daily`（主库） | 506 天、5,303 只、无残缺日；逐日 `name` 100% 非空，可按当日判 ST |
| `stock_daily`（history.db） | 1,230 天、5,108 只；**无退市股**（0 只最后日期显著早于 2026 年） |
| `sector_daily` | 255 天；`up_count/down_count` 全空；`net_inflow/member_count` 仅 724 行 |
| `sector_member` | 只有 5 天、每天 1~19 个板块，**高度残缺** |
| `rps_daily` | 只有 2 天；10-21 的 `ret50/rps50` 全空，证明历史混版本 |
| `pattern_hit` | 26 个有命中日，不连续 |
| `sector_fund_flow` | 只有 2 天 |

**关键结论**：主库日线足以做 5~60 日信号研究；长历史只能标注「现存股票样本研究」；板块→个股历史验证在 60 日回补前不可做。

---

## 4. 成分股回补质量（摘要）

完整报告：[2026-11-08-b0-sector-member-trial.md](2026-11-08-b0-sector-member-trial.md)

| 项 | 结果 |
| --- | --- |
| 隔离库 | `backend/data/b0_trial.db`，SQLite 在线备份 API 复制 |
| 试验 1 | 2026-09-24、5 个板块，`complete: 5`，2,275 行，6.3 秒 |
| 试验 2 | 2026-09-23、20 个板块，`complete: 20`，5,935 行，16.5 秒 |
| 来源对账 | 25/25 板块 `reported == unique == written` |
| 幂等 | 重跑 09-24 的 5 个板块：0 请求、0.0 秒 |
| 断点续跑 | manifest 按 (日期, 板块) 记录，互不覆盖 |
| 历史板块目录 | 目标取当天 `sector_daily`，不取当前 `sector_basic` |
| 60 日回补 | **未执行**；预计 5~8 小时；需分批批准 |
| 120 日回补 | **未执行** |

---

## 5. RPS 字段扩展与迁移（摘要）

完整报告：[2026-11-08-b0-rps-extension.md](2026-11-08-b0-rps-extension.md)

- 新增 `ret20/ret60/rps20/rps60`，旧三档含义不变；
- `scan_rps.WINDOWS=(20,50,60,120,250)`；
- 迁移脚本支持 `--apply/--rollback/--status/--backup`，默认拒绝主库；
- 2026-10-09 新旧值对照：5,115 只共同代码、六列 **0 字段差异**；
- 回补脚本按日 upsert，幂等；覆盖率分母用「目标日前 20 日中位数」估计；
- 250 日回补结果见 §8。

---

## 6. 统一回测架构（摘要）

完整说明：[2026-11-08-b0-backtest-kernel.md](2026-11-08-b0-backtest-kernel.md)

- 第一层信号研究 + 第二层成交模拟已实现；第三层组合回测只留边界；
- 默认 T+1 开盘成交；停牌顺延、开盘涨停不可买、退出跌停不可验证、数据末端 pending；
- 收益用 `pct_chg` 复利，处理除权除息；
- 基准 = 同入场日/退出日全市场等权，剔除入场涨停与退出跌停；
- 0/15 bps 两档费用；MAE 为收盘累计收益最小值；
- 市场环境按信号日分进攻/震荡/防守/未知；
- 13 项内核测试覆盖入场、停牌、不可买、跌停、费用、基准、分布、前视、复现、除权、ST。

---

## 7. 基线策略复测（摘要）

完整报告：[2026-11-08-b0-baseline-results.md](2026-11-08-b0-baseline-results.md)

| 策略 | 窗口 | 信号 | 5 日超额 | 20 日超额 | 60 日超额 | 结论 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| RPS60 前 50 | 2025-09-25~2026-03-31 | 5,735 | −0.36 | −2.22 | +1.01 | 无稳定正超额；60 日中位 −2.79、跑赢 39.6% |
| 趋势模板 | 同上 | 17,622 | −0.56 | −0.43 | +7.12 | 40/60 日出现正超额，但分布双峰、环境依赖强、70 日子窗口仅 +0.82；**待验证，不是结论** |
| 平台突破（长历史） | 2025-01-01~2026-10-19 | 10,384 | −0.26 | −0.61 | −1.20 | 全持有期负超额 |
| 箱体突破（长历史） | 同上 | 7,843 | −0.22 | −0.17 | −1.39 | 全持有期负超额 |
| 海龟突破（长历史） | 同上 | 30,382 | −0.17 | −0.46 | −1.25 | 全持有期负超额 |
| 板块叠加 | 2026-09-18~24 | 6 | −17.00 | — | — | **样本不足，不作结论** |

**结论**：

- RPS 与三个突破形态没有出现稳定的正超额；
- 趋势模板在 40/60 日出现正超额，但 60 日分布极端双峰（34% ≤−10%、40% >+10%）、「防守」环境贡献 +21.72、70 日子窗口仅 +0.82 —— **只能作为 B1 的待验证假设**；
- 2026Q1 样本外：RPS 40/60 日超额 +1.51/+8.90，趋势模板 +5.23/+14.20，方向与全样本一致；但 60 日中位仅 +0.38、跑赢率 49.2%、MAE −20.30 —— 均值由少数大赢家驱动，**不是高胜率策略，也还不是组合层可交易收益**；
- 板块叠加因数据不足无法判断；
- 负结果与不确定结果全部保留，不做口径修饰。

---

## 8. 测试与执行结果

### 8.1 基线（B0 开始前，master）

| 命令 | 结果 |
| --- | --- |
| 后端 `pytest -q` | **716 passed in 168.81s** |
| 前端 `npm run test` | 3 files / **21 passed** |
| 前端 `npm run lint` | 0 warnings / 0 errors |
| 前端 `npm run build` | 成功（572ms） |

### 8.2 B0 新增/修改测试

| 文件 | 结果 |
| --- | --- |
| `tests/test_scan_rps.py` | 7 passed |
| `tests/test_research_backtest.py` | 13 passed |
| `tests/test_backfill_sector_members.py` | 12 passed |
| `tests/test_migrate_rps_columns.py` | 1 passed |
| 合计新增/修改 | **33 passed** |

### 8.3 全量回归

| 命令 | 结果 |
| --- | --- |
| 后端 `pytest -q`（最终，全部修改后） | **746 passed in 178.63s**（基线 716 + 新增/修改 30） |
| 前端 `npm run test` | 21 passed（B0 未改前端） |
| 前端 `npm run lint` | 0 warnings / 0 errors |
| 前端 `npm run build` | 成功 |

B0 新增/修改测试单跑：**33 passed**；ruff 对全部新增/修改 Python 文件：**All checks passed**。

### 8.4 250 日 RPS 回补

| 项 | 结果 |
| --- | --- |
| 区间 | 2025-09-22 ~ 2026-10-09（250 个交易日） |
| 写入 | 1,269,453 行 |
| 跳过 | 0 天 |
| 耗时 | 约 31 分 19 秒（21:17:18 ~ 21:48:37） |
| 覆盖率分母（首日 → 末日） | 20 日 4,981→5,211；50 日 4,970→5,058；60 日 4,962→5,053；120 日 4,966→5,045；250 日 4,917→4,997 |
| 2026-10-09 旧值一致性 | 5,115 只共同代码，旧六列 0 字段差异 |

---

## 9. 尚存的数据局限与回测偏差

1. **退市股缺失**：`history.db` 只有当前代码池，0 只退市股；所有长历史结果必须标注「现存股票样本研究」。
2. **历史市值缺失**：生产的总市值 2000 亿过滤无法复现。
3. **ST 历史**：逐日名称可用，但 125 只票全历史名称恒为当前名，无法区分历史 ST 变化。
4. **板块成分**：本地只有 5 天且残缺；60 日回补未执行；板块内部广度、板块资金一致性、板块→个股历史验证均不可做。
5. **资金流**：只有 2 天，不能做历史一致性。
6. **RPS 分母**：每个窗口分母不同，且包含 ST/BSE；无真实历史成分股。
7. **形态历史**：`pattern_hit` 只有 26 天，不能直接做 60 日验证；长历史结论来自 `backtest_patterns.py` 重扫。
8. **成交假设**：日线无法确认逐笔成交；停牌顺延、涨停/跌停只能按日线近似。
9. **组合层**：未实现资金、仓位、持仓冲突、风险管理。

---

## 10. 进入下一阶段的建议与待批准事项

### 10.1 建议

B0 的工程目标已达到：数据覆盖可审计、回补可验证、RPS 可复现、回测可追溯、负结果如实保留。但**策略有效性没有一致通过**：

- RPS 与三个突破形态在已测窗口没有稳定正超额；
- 趋势模板在 40/60 日出现正超额（+2.42 / +7.12），但 60 日分布极端双峰、环境依赖强、70 日子窗口只有 +0.82，**只能算待验证假设**；
- 板块叠加因数据不足无法判断；
- 因此 **P0-1~P0-4 不应以「推荐/买入」形态上线**，只能以「研究视图 + 历史验证 + 明确失效条件」形态开发。

建议进入 P0 前先做两件事：

1. 完成 60 日 `sector_member` 回补（分批、隔离库），再决定 P0-4 的历史验证是否可用；
2. 把 B0 内核接到 P0-1（板块状态）、P0-2（趋势池）、P0-3（低位启动）的 `track` 接口，保持「候选 + 验证」成对交付。

### 10.2 待批准

| # | 事项 | 说明 |
| --- | --- | --- |
| 1 | 60 日 `sector_member` 回补 | 预计 5~8 小时；建议 5~10 天一批，隔离库执行 |
| 2 | 120 日回补 | 本轮不执行；60 日验收后再决定 |
| 3 | 是否允许把 B0 内核接入 P0 接口 | 先接 `track`，不新建页面 |
| 4 | 是否接受当前混合结果作为 P0 设计前提 | 页面必须显示样本数、区间、基准与失效条件；趋势模板 40/60 日的正超额需先做样本外验证 |
| 5 | 是否需要在 B0 后补做 120/250 日 RPS 窗口基线 | 当前 RPS 回补 250 日完成后可扩大样本期 |

---

## 11. 生产安全声明

- 未连接生产服务器；
- 未部署、未重启服务；
- 未修改生产数据库；
- 未修改线上定时采集任务；
- 所有历史回补均在 `backend/data/b0_trial.db` 隔离副本执行；
- 未推送 master / 未合并发布分支。
