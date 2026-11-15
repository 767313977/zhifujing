# Trend Engine V1.0 — M1～M6 总验收报告

- 日期：2026-11-15
- 分支：`feat/trend-engine-v1`（从 `f921aed` 切出）
- 范围：M1 指标层 / M2 Model A / M3 Model B / M4 板块与市场 / M5 风险引擎 / M6 回测
- 生产：**零触碰**。未安装 `data_revision`、未写任何研究库或生产库、未改调度、未部署、未合并 master。
- 研究数据：全程 `mode=ro` + `PRAGMA query_only=ON`。

---

## 1. 交付清单

### 1.1 代码

| 文件 | 说明 |
| --- | --- |
| `backend/app/services/trend_engine/__init__.py` | 包入口与版本 |
| `backend/app/services/trend_engine/config.py` | 全部阈值、权重、版本 |
| `backend/app/services/trend_engine/indicators.py` | MA/斜率/ATR/回撤/距高点/量能/数据质量（纯函数） |
| `backend/app/services/trend_engine/trend_structure.py` | 均线族、排列、收敛、拐头 |
| `backend/app/services/trend_engine/momentum.py` | RET5~250、RPS 读数与改善 |
| `backend/app/services/trend_engine/volume_price.py` | 量价结构、背离、异常放量 |
| `backend/app/services/trend_engine/scoring.py` | 分项映射、加权总分 |
| `backend/app/services/trend_engine/explanation.py` | 原因/风险/价位格式化 |
| `backend/app/services/trend_engine/breakout_model.py` | Model A |
| `backend/app/services/trend_engine/continuation_model.py` | Model B |
| `backend/app/services/trend_engine/screening.py` | 排序、TopN（不补足）、池子归属 |
| `backend/app/services/trend_engine/sector_strength.py` | 板块强度 |
| `backend/app/services/trend_engine/market_regime.py` | BULL/SIDEWAYS/BEAR/EXTREME_RISK/UNKNOWN |
| `backend/app/services/trend_engine/risk_engine.py` | NORMAL/WARNING/BROKEN |
| `backend/app/services/trend_engine/data_quality.py` | F2 RPS 日闸门 |
| `scripts/backtest_trend_engine.py` | M6 统一回测 CLI（只读） |

### 1.2 修改的既有文件

| 文件 | 改动 |
| --- | --- |
| `backend/app/services/research_screen.py` | F4 中位秩、F5 覆盖率闸门、F3 距高点统一 |
| `backend/app/services/pools.py` | F3 距最高收盘统一走 `trend_engine.indicators` |
| `backend/app/services/research_backtest.py` | `BarMatrix` 增加 `volume`（向后兼容） |

### 1.3 文档与证据

| 文件 | 内容 |
| --- | --- |
| `reports/trend_engine/00-architecture-audit.md` | M0 审计（已提交） |
| `reports/trend_engine/P0_TRACKER.md` | F1～F5 闭环 |
| `reports/trend_engine/stages/01-m1.md` ~ `06-m6.md` | 阶段报告 |
| `reports/trend_engine/m6/` | b1 主样本回测（JSON + MD） |
| `reports/trend_engine/m6_history/` | 长历史对照（JSON + MD） |
| `docs/trend_engine/01-indicators.md` | 指标口径 |
| `docs/trend_engine/02-models.md` | 双模型设计 |
| `docs/trend_engine/03-runbook.md` | 运行手册 |
| `docs/trend_engine/04-data-design.md` | M7/M8 数据设计（未实施） |

### 1.4 测试

| 范围 | 结果 |
| --- | --- |
| 新增单测 | 7 个文件、约 60 项：指标 / A / B / 板块市场 / 风险 / 数据质量 / 回测脚本 |
| 定向回归 | 149 passed |
| 后端全量 | **1827 passed, 4 skipped**（383 秒，2026-11-15） |
| Ruff | 改动文件全部通过 |

覆盖重点：平台高点排除当日、量能分母不含当日、缺失不填 0、并列同分、覆盖率闸门、
MA60 例外标记、BROKEN 多条件确认、PIT（追加未来数据后同一日结果逐字一致）、
只读连接拒绝写入、TopN 不补足、组合同日同票去重。

---

## 2. 回测结论（b1 主样本 + history 对照）

| 指标（20 日） | Model A | Model B | A+B |
| --- | ---: | ---: | ---: |
| b1 全区间超额 | -0.63 | -0.68 | -0.98 |
| b1 样本外超额 | -3.90 | -1.36 | — |
| history 全区间超额 | -0.61 | -0.58 | -0.99 |
| history 样本外超额 | -1.18 | +1.01 | — |

Walk-forward 四折全部为负（A -3.22 / -2.96；B -5.80 / -1.36）。
日期聚类 bootstrap 的 95% 区间均跨 0。

---

## 3. 最终十问

| # | 问题 | 结论 |
| --- | --- | --- |
| 1 | Model A 是否具备独立研究价值？ | **否**：两个数据集全区间与样本外均为负超额，WFA 两折为负 |
| 2 | Model B 是否具备独立研究价值？ | **否**：全区间为负；history 样本外 +1.01 但训练 +0.64 / 验证 -8.92，不稳定 |
| 3 | A+B 是否优于单模型？ | **否**（-0.98 / -0.99，均低于单模型） |
| 4 | RPS 是否带来正向增益？ | **无实质增益**：A -0.01pp、B +0.15pp（阈值 0.2pp）；history 无 RPS 不适用 |
| 5 | 板块因子是否带来正向增益？ | **无实质增益**：A -0.01pp、B +0.02pp（仅 60 天窗口，样本 422/447） |
| 6 | 哪种环境最适合？ | b1 指数口径：**BULL**（20 日超额 +2.71，样本 713）；仅作分层参考 |
| 7 | 哪种环境最容易亏损？ | **BEAR**（-5.42，样本 541） |
| 8 | 风险引擎是否降低最大回撤？ | **NOT_VALIDATED**：统一内核只支持固定持有期，风险退出未模拟 |
| 9 | 是否存在严重尾部依赖？ | **是**：A 40 日前 5% 收益 = 总收益 17.79 倍，剔除后 20 日均值 -2.45% |
| 10 | 是否具备进入 M7 的条件？ | **否**：样本外超额为负、bootstrap 下界 -6.43、数据缺口未补 |

---

## 4. 数据缺口与限制

1. `b1_research.db`：`breadth_daily` 为空；`index_daily` / `sector_daily` 止 2026-09-24；
   RPS 含混版本日（2026-10-21）。
2. `history.db`：无 RPS / 板块 / 指数 / 日历；**0 只退市股** → 幸存者偏差。
3. 生产只有约 34 个交易日 RPS 历史，不具备独立 Walk-forward 条件。
4. 没有历史 ST 状态表与退市名单；研究层用当日 `name` 判 ST，回测脚本同样使用当日名称。
5. 风险退出、组合层资金/仓位/冲击成本未实现，相关结论一律 `NOT_VALIDATED`。

---

## 5. 进入 M7 前必须完成

1. 修复并补齐数据：指数/板块采集持续到与个股同日；`breadth_daily` 重算；
   RPS 用当前版本重算目标区间（F2 闸门已在读取侧生效）。
2. 在隔离库安装 `data_revision` 并扩展 `trend` 域（F1，需单独授权）。
3. 重新设计模型（当前 A/B 在趋势友好与不友好区间均无稳定超额），
   或先做行业中性化 + purged walk-forward 再评估。
4. 风险退出加入成交模拟（ATR 止损 / 结构失效退出），验证是否降低回撤。
5. M6 结论达标（样本外 >0、bootstrap 下界 >0、样本 ≥200）后才允许 M7 的实施。

---

## 6. Git 提交与推送记录

分支：`feat/trend-engine-v1`（远端已同步）

| 提交 | 内容 |
| --- | --- |
| `1651cf2` | M0 架构审计 |
| `a5462ad` | M1 指标基础设施 |
| `a18f0a1` | M2 Model A |
| `a6a5295` | M3 Model B |
| `469ede9` | M4 板块分位统一、覆盖率闸门、数据质量闸门 |
| `8dfe433` | M5 风险引擎 |
| `dbf7dd2` | M6 统一回测与验证报告 |

工作区仅保留一个**既有**未提交改动 `reports/s6/s62/migration-report.json`（非本批次产生，未触碰）。

---

## 7. 生产安全声明

- 未连接生产服务器；
- 未写任何数据库（所有会话 `mode=ro` + `query_only`）；
- 未安装 `data_revision`、未改生产调度、未改生产配置；
- 未部署、未合并 master、未 push 到 master；
- 所有回测在本地 `b1_research.db` / `history.db` 只读副本语义下完成。
