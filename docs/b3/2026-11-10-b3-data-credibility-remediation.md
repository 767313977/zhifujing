# B3 数据可信度整改与首轮实验准备

- 日期：2026-11-10
- 范围：幸存者偏差核查、跨库访问消除、历史 ST / 流动性前视、RPS 口径、基线参数纠错
- 未做：未跑 5 年全市场实验；未改默认选股参数；未读 35 个敏感库；未申请外部数据
- 状态：`STRATEGY_PERFORMANCE_NOT_VERIFIED`、`PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED`、`P13_RELEASE_NO_GO`

---

## 1. 修正后的数据可用性结论

| 结论 | 依据 |
| --- | --- |
| `history.db` 是**唯一**可长周期研究的行情源 | 829 MB，1300 根 ≈ 2021-09 ~ 2026-10-19 |
| 实际代码数 **5,108**（不是 4,922） | 本次只读清点 `SELECT DISTINCT code`；4,922 是旧文档中「按 ≥250 根过滤后」的数量 |
| `history.db` 的票来自 `backfill_history.py` 的 `load_codes()` | 即**当时的当前池**，退市/出池票整段缺席 |
| 换用 history.db 取码后，样本比当前池多 186 只 | 说明回补时点与当前池不完全一致，但**仍非历史全市场** |
| 逐日流通市值、历史首封时间、5 年板块成员 | 仍缺失，不得近似替代 |

---

## 2. 幸存者偏差与历史 ST 风险（确认存在）

### 2.1 逐条回答

| 问题 | 结论 |
| --- | --- |
| 4,922 只是否来自当前股票池 | **是**。旧路径 `load_codes()` = `SELECT code FROM stock_universe`（当日快照） |
| 退市股票是否遗漏 | **遗漏**。退市票不在当前池，也不在回补列表里 |
| 历史 ST 是否按当前状态提前剔除 | **部分**。`_st_rows` 已改为逐日按 `stock_daily.name`；但回补库同一票名称可能整段等于当前名称，无法还原 |
| 2021 年未上市公司如何处理 | 该日无行情行 → 不产生信号（正确）；但「后来上市」的票在上市前不会被计入横截面分母（正确），上市后才进入 |
| RPS 分母是否含当时真实可交易股票 | **不完整**。分母 = 当天该窗口有值的票，而票集合本身是回补池（缺退市股）→ 排名偏乐观 |
| 历史基准是否同样偏差 | **是**。`_benchmark()` 与样本共用同一 `grouped` 池，同向偏差 |

### 2.2 定级

- 趋势 / 形态 / 低吸的历史结果：**`EXPLORATORY_ONLY`**（存在幸存者偏差 + 历史 ST 不完整）。
- 不得称为「严格无偏差样本外回测」。
- 本轮**没有**通过修改收益公式掩盖该缺失。

### 2.3 已落地的显式标记

运行 `--history` 且未启用名称覆盖时，报告固定输出：

```
数据源：history.db（只读）；名称覆盖=未启用（未访问其他数据库）
⚠️ 历史 ST 状态：ST_STATUS_PARTIAL —— 回补库可能把当前名称写满整段历史，
   无法逐日还原；本报告不据此声称严格无偏差。
```

---

## 3. 回测代码的数据库访问路径

### 3.1 改造前（问题）

| 环节 | 连接 | 问题 |
| --- | --- | --- |
| `_load_history()` 末尾 | `_overlay_names_from_main()` → `session_scope()` → **主库 fupan.db** | 无条件、无开关、无提示；主库不存在时 SQLite 会新建空文件 |
| 股票池 | `load_codes()` → **主库 stock_universe** | 隐式依赖主库；且是当前池快照 |
| `history.db` | `create_engine(f"sqlite:///{path}")` | 可写、可在缺库时新建 |

### 3.2 改造后

| 环节 | 连接 |
| --- | --- |
| `--history` 主数据 | `history.db`，`mode=ro`（只读，不可写、不新建） |
| 股票池（`--history` 默认） | `history.db` 的 `SELECT DISTINCT code`，**不连主库** |
| 股票池（显式 `--codes-source main`） | 主库 `stock_universe`，日志明示"存在当前池前视与幸存者偏差" |
| 名称覆盖 | 仅 `--overlay-names-db PATH` 显式给定时；`mode=ro`；库不存在 → `SystemExit`，不创建 |
| 非 `--history` | 主库（原有行为，未改） |

新增统一入口 `_readonly_engine(path)`，三处调用共用，便于断言。

---

## 4. 策略基线参数核对（纠正 B3 第一节文档）

| 项 | 冻结代码中的真实值 | 原 B3 文档 | 处置 |
| --- | --- | --- | --- |
| `first_board(order=)` | 仅 `"board"`（默认，综合分）与 `"factor"` | 写成 `composite` | **纠错**：默认即 `board` |
| 综合分权重 | `SCORE_W_THEME=0.4`、`SCORE_W_SEAL=0.4`、`SCORE_W_TIME=0.2` | 未写 | 补充 |
| 平台突破 | `platform_breakout` | 写成 `box_breakout` | **纠错** |
| 新高突破 | `new_high`（创 N 日新高） | 写成 `gap_up_breakout` | **纠错** |
| 箱体突破 | `box_breakout` | 未单列 | 补列 |
| 跳空缺口突破 | `gap_up_breakout` | 未单列 | 补列 |
| `signals_from_rps` 数据源 | 读 `rps_daily` 表 + `_liquid_codes`（历史 `stock_daily.amount` 的 20 日均值，窗口为 D 之前） | 未说明 | 补充；**无前视**，但窗口不含 D 日 |
| `signals_from_trend_template` | RPS 前 `candidate_top=200`（`mode=rps60`）→ `trend.evaluate` | 「RPS60 前 200」正确 | 保持 |
| 历史成交额 | `stock_daily.amount` 可用；`history.db` 有 amount 列 | 已列 | 确认 |
| `rps_daily` 历史 | 混版本，不可直接拼接 | 已列 | 确认 |
| 形态独立 | `new_high` / `platform_breakout` / `box_breakout` / `gap_up_breakout` 是 4 个不同形态 | — | 不得合并统计 |

冻结版本与 `master` 差异：以上均按冻结分支 `feature/stock-screening-v2`（HEAD `1b40a4e`）核对。

---

## 5. 最小代码修复与测试

### 5.1 改动文件

| 文件 | 改动 |
| --- | --- |
| `scripts/backtest_patterns.py` | 新增 `_readonly_engine()`、`_codes_from_history()`；`_load_history(..., overlay_names_db=None)`；`_overlay_names_from_main(..., db_path: Path)` 显式只读；CLI 新增 `--codes-source`、`--overlay-names-db`；报告增加数据源与 `ST_STATUS_PARTIAL` |
| `tests/test_backtest_history_isolated.py` | 新增 10 例 |

### 5.2 测试

| 用例 | 断言 |
| --- | --- |
| `test_load_history_never_calls_main_session` | 默认路径调用 `session_scope` 即失败 |
| `test_load_history_creates_no_extra_file` | 目录文件集合前后一致 |
| `test_readonly_engine_is_mode_ro` | URL 含 `mode=ro` |
| `test_readonly_engine_cannot_write` | `DELETE` 抛 `OperationalError` |
| `test_overlay_missing_db_exits_without_creating` | `SystemExit` 且文件未创建 |
| `test_overlay_explicit_db_overrides_names` | 覆盖生效，源库字节不变 |
| `test_load_history_with_overlay_applies_names` | 仅重叠日期被覆盖 |
| `test_st_rows_marks_constant_names` | `constant_st` 计数正确 |
| `test_codes_from_history_uses_only_that_db` | 不触碰主库 |
| `test_codes_from_history_does_not_create_file` | 缺库不新建 |

### 5.3 结果

| 批次 | 通过 | 跳过 | 失败 |
| --- | ---: | ---: | ---: |
| 新增隔离测试 | 10 | 0 | 0 |
| RPS 相关（`test_scan_rps` + `test_import_rps_history`） | 36 | 0 | 0 |
| 后端全量 | **1019** | 3 | 0 |

RPS 口径的合成数据验证已有覆盖：`test_window_is_trading_days_not_rows`（窗口按交易日）、`test_skips_when_day_coverage_too_low`（90% 覆盖）、`test_stock_without_target_day_is_excluded`（分母剔除当日无行情）。

### 5.4 小规模试跑（只读）

```
python scripts/backtest_patterns.py --pattern new_high --history --stocks 8 --bars 250
```

| 观测 | 值 |
| --- | --- |
| 股票池 | history.db 内 5,108 只（未读取主库） |
| 数据源 | history.db（只读），窗口 250 根，截至 2026-10-19 |
| 交易日轴 | 250 个（2025-09-30 ~ 2026-10-19） |
| 命中 | 8 条 |
| 耗时 | 4.2 秒（8 只） |
| ST 标记 | `ST_STATUS_PARTIAL` 已输出 |

---

## 6. 可安全开展的首轮实验范围

| 实验 | 命令骨架 | 证据等级 |
| --- | --- | --- |
| 形态 × RPS 过滤（5 年） | `--history --pattern new_high,platform_breakout --filter-split rps50_90,rps120_85,rps_dual,trend_template --stocks 800` | `EXPLORATORY_ONLY` |
| 形态 × 成交额分层 | 同上 + 内存 amount 分层（待加参数） | `EXPLORATORY_ONLY` |
| 低吸因子 IC | `scripts/factor_ic.py`（5 年、4900 只） | `EXPLORATORY_ONLY` |
| 首板接力 | `scripts/backtest_relay.py`（255 日） | `LOW_POWER` + `EXPLORATORY_ONLY` |
| 板块→个股 | 不跑 | `INSUFFICIENT_EVIDENCE` |

首轮建议**先跑 800 只抽样**，测量内存与耗时，再决定是否全量。

### 6.1 首板双内核差异（未对账，禁止删除旧脚本）

| 维度 | `backtest_relay.py` | `research_backtest.run_signals()` |
| --- | --- | --- |
| 入场 | D+1 开盘 | `next_open` |
| 持有期 | 1 / 2 / 3 / 5 | 1 / 5 / 10 / 20（默认） |
| 一字板 | `_tradable()` 剔除并计数 | `_entry_limit_up()` 剔除，状态 `entry_limit_up` |
| 停牌 | 当日无行 → 跳过 | `_find_entry()` 顺延，`pending` |
| 跌停卖不出 | **未处理** | `_exit_limit_down()` 单独标记 |
| 成本 | 0（文档注明双边约 0.13%） | `cost_bps` |
| 基准 | 当日全市场等权（同窗口、剔买不到） | `Benchmark`（同 entry/exit、剔涨停/跌停） |
| 去重 | 不去重 | 形态信号 20 日去重 |

结论：两套内核在「跌停退出」与「成本」上语义不同，**当前不能逐笔对齐**。需要先实现 `signals_from_first_board()` 并把跌停退出纳入 relay，才符合第七节的逐笔对账前提。本轮未实现，也未删除 relay。

---

## 7. 仍需用户单独授权的数据访问

1. `b1_research.db`（1.37 GB）及其快照：只读跑覆盖率统计与计时。
2. Tushare Pro：补逐日流通市值、更早的涨跌停 / ST / 停牌字段。
3. 全市场 5 年日线回补（腾讯）：约 5,500 只 × 6 秒 ≈ 9 小时，用于降低幸存者偏差。
4. 首板内核统一改造（新增 `signals_from_first_board`）：会改动回测代码，需确认范围。

未获授权前，只做只读 `history.db` 的探索性小规模试跑。
