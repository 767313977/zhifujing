# B3 第二阶段：策略有效性研究的数据与基线方案（第一步）

- 日期：2026-11-10
- 阶段目标：找出有历史统计价值的选股条件，不新增技术指标
- 本轮范围：**只做第一步调研**，未运行任何新回测、未改默认参数、未连生产库
- 状态标记：`DATA_SOURCE_ASSESSED`、`BACKTEST_SCOPE_DEFINED`、`STRATEGY_PERFORMANCE_NOT_VERIFIED`

---

## 1. 可用历史数据来源与缺口

### 1.1 本地已有（无需外部下载）

| 数据集 | 位置 | 口径 | 覆盖 | 规模 |
| --- | --- | --- | --- | --- |
| 长历史日线 `stock_daily` | `backend/data/history.db` | 腾讯**不复权价 + 真实涨跌幅**，非北交所、已剔 ST | 1300 根 ≈ 2021-09-14 ~ 2026-10-19，4922 只 | 829 MB |
| 主库日线 | `backend/data/fupan.db` → `stock_daily` | iFinD 不复权 + 真实涨跌幅，保留 400 交易日 | 2024-09-02 ~ 2026-10-09，506 天，5303 只 | 490 MB |
| 涨停池 | `limit_pool` | akshare push2ex 当日快照 | 2025-09-08 ~ 2026-09-24，255 天，28,225 行 | 同主库 |
| 板块日线 | `sector_daily` | 开盘红自算板块涨跌（成分当日生效） | 2025-09-08 ~ 2026-09-24，255 天，374 板块 | 同主库 |
| 板块成分 | `sector_member` | **当时成分**回补，非今日名单回填 | 2026-07-24 ~ 2026-10-23，60 交易日，393.9 万行 | 见 `b1_research.db` 1.37 GB |
| RPS | `rps_daily` | pct_chg 复利 + 中位秩，含 20/50/60/120/250 | 需按当前版本重算目标区间 | 同主库 |
| 形态命中 | `pattern_hit` | 命中当日行情快照 | 仅 26 个有命中的交易日 | 20,637 行 |
| 研究隔离库 | `b0_trial.db` / `b1_research.db` / `b2_final_snapshot.db` | 主库副本 + 回补 | 60 天全板块成分股 100% 完成 | 663 MB / 1.37 GB / 1.37 GB |

> 敏感演练库 `backend/data/p13_audit/*.db`（18 个，含 `prod_replica.db`）**本轮未打开、未读取**，后续也不作为研究数据源。

### 1.2 逐字段可用性

| 字段 | 来源 | 可用性 | 未来信息风险 |
| --- | --- | --- | --- |
| 代码 / 交易日 | `stock_daily` 复合主键 | 可用，5 年 | 无 |
| 原始 OHLCV | `stock_daily.open/high/low/close/volume` | 可用，不复权 | 无 |
| 复权序列 | 由 `pct_chg` 逐日复利还原（`research_backtest.BarMatrix`、`patterns` 同口径） | 可用 | 无；但**不要**用「今日复权因子」回乘历史价 |
| 真实涨跌幅 | `stock_daily.pct_chg`（官方值） | 可用 | 无 |
| 成交额 | `stock_daily.amount` | 可用 | 无 |
| 换手率 | `stock_daily.turnover` | 主库有；`history.db` 需确认（腾讯源不一定给） | 无 |
| 流通市值（逐日全市场） | **缺口**：仅 `limit_pool.float_mv`（涨停股当日）与 `stock_universe.total_mv`（**当日快照**） | 部分可用 | `stock_universe` 是快照，拿它做历史筛选即前视，禁止 |
| 涨停价 / 涨停状态 | 由 `services.limit_rules` 按 `pct_chg` + 当日 `name` + 板块规则判定；`limit_pool` 只覆盖涨停/跌停/炸板股 | 可推导 | 无（不要用「今天的 ST 名单」判历史） |
| 历史 ST | `stock_daily.name` 的**当日值**（B0 已确认 506 天逐日 name 100% 非空、140 个 ST 简称、7 只改过简称） | 可用 | 无；**不得**用最新名称回填 |
| 停牌 | 「该日无行情行」 | 可用 | 无 |
| 退市 | 「最后一根日线显著早于全库最后日」近似 | 仅提示 | 不是严格退市标记 |
| 历史板块成员 + 生效日期 | `sector_member`，60 交易日 | 可用但**窗口很短** | 回补是当时成分，无前视；窗口外无数据 |
| 首封时间 / 封单额 | `limit_pool.first_seal_time` / `seal_amount` / `open_times` / `consecutive` | 255 天 | 无；但 `limit_pool.industry` 跨日混两套分类体系，**不可跨日分组** |

### 1.3 明确缺口（不得用近似值冒充）

1. **全市场逐日流通市值**缺失 → 任何「按市值分层」的结论只能限于涨停股样本，不能外推到全市场。
2. **通联/同花顺级历史板块成员与生效日期**只有 60 天 → 「板块→个股」只能做 60 天，无法做 5 年样本外。
3. **`pattern_hit` 只有 26 个命中日** → 落库形态不可直接回测；必须用 `scripts/backtest_patterns.py --history` 在内存中重算（该路径已在用，5 年 / 4922 只）。
4. **`rps_daily` 历史混版本**（2026-10-21 的 `ret50/rps50` 全空）→ 跨日期研究必须先用当前版本重算目标区间，并在报告中记录版本与分母。
5. **历史首封时间/封单无更早来源** → 首板研究上限 255 个信号日。

### 1.4 外部公开数据方案（本轮不下载）

| 来源 | 授权方式 | 可补字段 | 已知限制 |
| --- | --- | --- | --- |
| 腾讯行情（项目在用） | 公开 HTTP，无需密钥 | 不复权 + 前复权日线，3 年/743 行、10 年/2444 行 | 约 6 秒/只，4 线程最优；无换手率/市值 |
| AKShare | 开源库，免费 | 日线、涨停池、板块成分、ST 名单 | 东财 `push2his` 在本机 HTTP 层被重置（2026-09-25 实测）；接口随时可能变 |
| Tushare Pro | 注册 + 积分，需用户授权 | 复权因子、日线、涨跌停、ST、停牌、板块成员 | 积分门槛；**需用户明确同意后才可申请与下载** |
| iFinD（项目已接） | 商业授权，配额制 | 全字段 | 采集一天 13 次调用；历史回补成本高 |

**结论**：在用户书面确认前，不申请 Tushare Token、不批量下载任何外部数据；研究先跑在本地 `history.db` + 60 天板块回补上。

---

## 2. 现有回测函数复用方案

### 2.1 已有内核（直接复用，不改动）

| 能力 | 位置 | 说明 |
| --- | --- | --- |
| 统一回测入口 | `services/research_backtest.run_signals()` | 参数：`horizons=(1,5,10,20)`、`entry_mode=next_open/close`、`cost_bps`、`max_entry_delay`、`max_exit_delay`、`benchmark_mode`、`matrix`/`benchmark` 复用 |
| 行情矩阵 | `BarMatrix` + `load_matrix()` | 5000 只 × 300 天 ≈ 150 万格，numpy 约 70–90 MB；`pct_chg` 复利还原前复权 |
| 基准 | `Benchmark` | 同一 (入场日, 退出日) 全市场等权，剔除入场涨停 / 退出跌停 |
| 可成交判定 | `_entry_limit_up` / `_exit_limit_down` | 次日一字板 = 买不到，单独计数 |
| 信号：RPS | `signals_from_rps(mode=..., top=..., min_rps=..., min_avg_amount=...)` | 支持 50/120/250 |
| 信号：形态 | `signals_from_pattern(patterns=...)` | 读 `pattern_hit`，样本仅 26 天 |
| 信号：趋势模板 | `signals_from_trend_template(candidate_top, mode, min_rs)` | 与 `/pools/rps?trend_template=true` 同一份 `trend.evaluate` |
| 信号：板块→个股 | `signals_from_sector_stock(taxonomy, sectors, per_sector)` | 依赖 `sector_member`，仅 60 天 |
| 长历史形态 | `scripts/backtest_patterns.py --history --filter-split rps50_90,rps120_85,rps_dual,trend_template` | 5 年、4922 只、复用生产形态函数，支持 `--entry next_open`、`--skip-unbuyable`、`--stocks N` 抽样 |
| 首板接力 | `scripts/backtest_relay.py` | 直接调 `pools.first_board`，`COMBOS` 已覆盖首封时间/市值/题材家数/真首板/封单比；剔除次日一字板 |
| 因子 IC | `scripts/factor_ic.py` | rev5/rev20、amp20、illiq20（Amihud），5 年、4900 只 |

### 2.2 复用缺口与建议（本轮不实现）

1. **首板未接入统一内核**：`backtest_relay.py` 用自有 `Tally`（持有期 1/2/3/5、逐样本超额），`run_signals` 用 `Sample`（1/5/10/20、MAE、pending 状态）。
   → 建议新增 `signals_from_first_board()` 接入 `run_signals`，使首板与趋势/低吸共享同一套持有期、成本、基准与样本外切分；**不删除** relay 脚本作为独立对账。
2. **`history.db` 无 `trade_calendar`**：`factor_score.scores()` 依赖日历 → 因子实验走纯函数 `score_series()` + 内存 bars，已在 `backtest_patterns` 内处理。
3. **`sector_member` 只有 60 天**：`signals_from_sector_stock` 的样本外无法与 5 年趋势研究对齐，需单列结论。
4. **`history.db` 的 `turnover`、`float_mv` 未确认**：若缺失，涉及换手/市值的策略只能退回主库 506 天窗口。

---

## 3. 三类策略的基线定义（参数取当前生产默认，不修改）

### A. 趋势突破

| 变体 | 参数 |
| --- | --- |
| RPS250 强度榜 | `mode=all, min_rps=90`，同时要求 `rps250`/`rps120` ≥ 90，按 RPS250 降序，前 50 |
| 双高 | `mode=dual`，`RPS50 ≥ 90 且 RPS120 ≥ 85` |
| 一线红 | `mode=line_red`，任一档 ≥ 90，按三档最大值排序 |
| 趋势模板 | RPS60 前 200 候选中，`trend.evaluate`（Minervini）通过者，`min_rs=70` |
| 平台突破 / 新高突破 | `backtest_patterns --history --pattern box_breakout / gap_up_breakout`，同一票 20 日去重 |
| 成交额门槛 | `stock_universe.avg_amount` 与 `min_avg_amount` 参数 |

单独使用与组合使用分别测量（RPS × 形态、RPS × 趋势模板、形态 × 成交额）。

### B. 首板接力

| 变体 | 参数 |
| --- | --- |
| 默认 | `before=140000`、`min_float_mv=20`、`max_float_mv=300`、`min_themes=3`、`order=composite` |
| 首封时间 | `before = 103000 / 113000` |
| 市值 | `30~150 亿` |
| 题材家数 | `min_themes ≥ 5` |
| 真首板 | `only_true_first_board=True` |
| 封单比 | `min_seal_ratio=0.3`（占流通市值 %） |
| 排序 | `composite` vs `factor` |

必测约束：次日竞价封死（一字板）→ 剔除并计数；停牌 → 无行情行；持有 1/2/3/5 日。

### C. 低吸反转

| 因子 | 定义 |
| --- | --- |
| `rev20` | 20 日复利涨幅取负 |
| `amp20` | 20 日平均振幅 `(high-low)/close` |
| `illiq20` | 20 日平均 `|pct| / 成交额(亿)`（Amihud） |
| `rev5` | 5 日反转（对照） |

不改变现有权重；按流动性分层（成交额分位）分别报告收益与成本。

---

## 4. 数据规模与性能预算

| 项 | 量级 | 依据 |
| --- | --- | --- |
| 5 年日线行数 | ≈ 4900 只 × 1230 日 ≈ 600 万行 | `history.db` 829 MB |
| 单次矩阵内存 | 300 天 ≈ 70–90 MB；1230 天线性外推 ≈ **300–360 MB** | `BarMatrix` 注释 |
| 板块成分 | 393.9 万行 / 60 天，1.37 GB 库 | B1/B2 验收 |
| 首板 | 255 信号日，2547 候选 | `回测_涨停接力` |
| 形态长历史 | 8 形态 1000 只 ≈ 208 秒；4922 只 8 形态 ≈ 17 分钟 | `回测_RPS与趋势模板` |
| 单次 `run_signals` | **UNMEASURED**（待实测）；矩阵加载预计占主导 | 需在授权库上计时 |
| 参数敏感性 M 组 | 复用同一 `matrix` + `benchmark`，只付一次加载成本 | 代码支持 |

预算建议：单次研究控制在「矩阵常驻内存 + 20 组以内参数」，整机内存预留 2 GB 以上。

---

## 5. 可开展严格样本外回测的范围

| 方向 | 训练区间 | 样本外 | 判定 |
| --- | --- | --- | --- |
| A 趋势突破（RPS / 双高 / 一线红 / 趋势模板） | 2021-09 ~ 2024-12 | 2025-01 ~ 2026-10 | **可做**，样本充足 |
| A 形态突破（平台 / 新高） | 2021-09 ~ 2024-12 | 2025-01 ~ 2026-10 | **可做**，需 `--history` 重算 |
| B 首板接力 | 2025-09 ~ 2026-03 | 2026-04 ~ 2026-09 | **受限**：总 255 日，样本外约 100 日，结论需标 `LOW_POWER` |
| C 低吸反转（rev20/amp20/illiq20） | 2021-09 ~ 2024-12 | 2025-01 ~ 2026-10 | **可做**（IC 口径） |
| D 板块→个股 | 2026-07-24 ~ 2026-08-31 | 2026-09-01 ~ 2026-10-23 | **不做**：60 天总量，标 `INSUFFICIENT_EVIDENCE` |
| 板块共振（`sector_daily` 口径） | 2025-09 ~ 2026-03 | 2026-04 ~ 2026-09 | 可做但仅 255 天，标 `LOW_POWER` |

参数纪律：只在训练区间选参数；样本外结果只用于验证，不回流调参。

---

## 6. 本轮未做与待确认

**未做**：未运行新回测；未修改任何默认参数；未下载外部数据；未打开 35 个敏感库；未提交策略代码。

**待用户确认后执行**：

1. 是否允许在 `b1_research.db`（1.37 GB 隔离库）上跑第一步计时与覆盖率统计（只读、不写入）。
2. 是否需要引入 Tushare Pro 补「逐日流通市值」与「更早的涨停/ST 字段」；若需要，请给出授权范围。
3. 是否同意把首板接入统一内核（新增 `signals_from_first_board`），以统一持有期与成本口径。
