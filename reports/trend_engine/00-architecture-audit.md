# Trend Engine V1.0 — M0 全局架构审计

- 版本：M0 / 2026-11-15
- 范围：只读审计。未修改任何代码、未写入任何数据库、未部署、未合并 master。
- 审计对象：`zhifujing`（致富经）当前开发分支与本地研究库快照。
- 结论摘要：**现有 RPS / 形态 / 板块 / 回测 / 调度能力足以支撑双趋势模型，不需要重写；
  但有 5 个 P0 级口径问题必须先修，且既有研究证据与任务书的两处权重设定直接冲突
  （RPS 硬过滤、板块加分），必须在评分层做成可关闭、可回测的开关。**

---

## 1. 基线状态

### 1.1 Git

| 项 | 实测值 |
| --- | --- |
| 当前分支 | `work/s6-8-s6-9-gate-closure`（跟踪 `origin/work/s6-8-s6-9-gate-closure`） |
| HEAD | `f921aed docs(s6.12): record production upgrade execution` |
| 与 `master` 的关系 | **落后 0 / 领先 174**（`git rev-list --left-right --count master...HEAD` = `0 174`） |
| 与 `origin/master` 的关系 | 同上（本地 `master` 与 `origin/master` 指向同一提交 `44f0912`） |
| 工作区 | 1 个既存未提交改动：`reports/s6/s62/migration-report.json`（**非本次产生，未触碰**） |
| 本地分支数 | 17（含 `feat/b0-research-foundation`、`feat/b1-research-validation`、`feat/b2.1-full-backfill`、`feat/p0.2-research-ui`、`release/s6-canary-*` 等） |

**含义**：`master` 不含 B0/B1/B2/P0.2/P0.3/S6.x 共 174 个提交，**不能以 master 为开发基线**。
趋势引擎分支必须从当前 HEAD `f921aed` 切出，否则会丢失研究缓存、数据修订、只读边界等全部前置能力。

### 1.2 生产版本与开发版本

| 项 | 实测/记录值 | 来源 |
| --- | --- | --- |
| 生产站点 | `https://zhifujing.top`，`/opt/fupan`（**无 `.git`**） | `reports/s6_12/08-production-upgrade-report.md` |
| 生产升级时间 | 2026-11-15 18:21:35 CST，停机 2 分 57 秒，未回滚 | 同上 |
| 生产代码指纹 | `main.py` SHA256 `792dfcb2…`；发布包 SHA256 `447af533…` | 同上 |
| 生产 `stock_daily` | 2,627,509 行，最新 **2026-11-13** | 同上 |
| 生产 `rps_daily` | 178,873 行 | 同上 |
| 生产调度 | 15:05 主采集、17:30/19:30/22:35 晚间、22:00/22:30/08:30 成分股、09:15–09:26+09:31 竞价、20:00 告警、14:50 B1 盘中预览、23:10 备份 | 同上 + `backend/app/config.py:219-487` |
| `/api/ready` | 503（fail-closed 安全门禁，监控用 `/api/health`） | 同上 |

**推算（非实测）**：178,873 行 ÷ 约 5,200 只/日 ≈ **34 个交易日**的 RPS 历史。
生产端不足以独立完成 60 日以上 Walk-forward，趋势模型的历史验证必须使用隔离研究库。

### 1.3 本地数据库清单

| 文件 | 大小 | 用途 |
| --- | --- | --- |
| `backend/data/b2_final_snapshot.db` | 1,374.6 MB | B2 最终研究快照（本次审计主对象） |
| `backend/data/b1_research.db` | 1,374.6 MB | B1 研究库，与 b2 数据面基本一致 |
| `backend/data/history.db` | 829.5 MB | 5 年长历史，**只有 `stock_daily` 一张表** |
| `backend/data/fupan.db` | 489.7 MB | 默认 `DB_PATH`（`config.py` 的 `DATA_DIR/"fupan.db"`），RPS 仅 2 天 |
| `backend/data/b0_trial.db` | 662.6 MB | B0 试验库 |
| `backend/data/forward_research.db` | 63.9 MB | 前瞻纸面跟踪库（只有 `stock_daily` + 3 张元表） |

### 1.4 数据覆盖矩阵（b2 快照，只读实测）

| 表 | 行数 | 起 | 止 | 天数 | 判定 |
| --- | ---: | --- | --- | ---: | --- |
| `stock_daily` | 2,547,042 | 2024-09-02 | 2026-10-09 | 506 | **可用**；逐日 5,200+ 行，`pct_chg` 无残缺 |
| `rps_daily` | 1,274,587 | 2025-09-22 | 2026-10-21 | 251 | **可用但有混版本**（见 F2） |
| `sector_daily` | 94,771 | 2025-09-08 | 2026-09-24 | 255 | 可用；`up_count/down_count` 全 NULL |
| `sector_member` | 3,646,796 | 2026-07-03 | 2026-09-24 | 60 | 可用；275 个板块/日 ≈ 260 个，接近全量 |
| `sector_member_backfill` | 16,190 | 2026-07-03 | 2026-09-24 | 60 | 回补 manifest（板块-日） |
| `pattern_hit` | 20,637 | 2026-05-14 | 2026-10-09 | **26** | 仅 26 个命中日，不能当连续信号源 |
| `index_daily` | 1,530 | 2025-09-08 | **2026-09-24** | 255 | **比个股末日落后 9 个交易日** |
| `market_sentiment` | 255 | 2025-09-08 | 2026-09-24 | 255 | 情绪可用，同样落后 |
| `fear_greed_daily` | 254 | 2025-09-09 | 2026-09-24 | 254 | 可用，同样落后 |
| `breadth_daily` | **0** | — | — | — | **空表**；市场宽度需由 `collect_breadth` 重算 |
| `limit_pool` | 28,225 | 2025-09-08 | 2026-09-24 | 255 | 可用 |
| `stock_basic` | 5,572 | — | — | — | `industry` 100% 非空（**当前值，非历史值**） |
| `stock_universe` | 5,261 | — | — | — | **当前池**，非历史成分 |
| `trade_calendar` | 8,797 | 1990-12-19 | **2026-12-31** | — | **预置了未来日期** |
| `history.db` | 5,820,960 | 2021-09-14 | 2026-10-19 | 1,230 | 只有日线，5,108 只，**无退市股** |
| `forward_research.db` | 222,503 | 2026-09-09 | 2026-11-11 | — | 无 RPS/板块/指数/形态，**不能做趋势回测** |

**关键约束**：
1. 个股日线到 10-09，指数/板块/情绪到 09-24，RPS 到 10-21 → **三套末日不一致**；
2. `breadth_daily` 为空 → 任务书第六阶段（市场环境）不能直接读现成表；
3. `trade_calendar` 有未来日期 → 所有「未来 N 日」计算必须以**数据末日**封顶；
4. `sector_member` 只有 60 天 → 板块广度与「板块内强势股数量」最长只能回溯 60 天。

---

## 2. 现有能力清单（文件级）

### 2.1 指标层

| 能力 | 文件 | 关键函数 | 口径要点 |
| --- | --- | --- | --- |
| RPS 纯函数 | `backend/app/services/rps.py` | `ret` / `window_values` / `mid_rank` / `percentiles` | `pct_chg` 复利、交易日截窗、90% 覆盖、中位秩 |
| RPS 落库 | `backend/app/jobs/scan_rps.py` | `scan` | 5 窗口（20/50/60/120/250）、覆盖率闸、过期单元格显式清 NULL |
| 前复权 K 线 | `backend/app/services/patterns.py` | `usable_bar` / `build_bars` / `Bars` | 不复权价 + `pct_chg` 复利；停牌残行剔除 |
| 形态引擎 | `backend/app/services/patterns.py` | `PATTERNS` / `_volume_ratio` / `classify_phase` | 45+ 形态、连续评分、阶段判定 |
| 趋势模板 | `backend/app/services/trend.py` | `passes` / `evaluate` / `load_bars` | Minervini 8 条，250 日窗口，与回测共用 |
| 研究指标 | `backend/app/services/research_screen.py` | `trend_rows` / `low_start_rows` / `sector_states` | **现有「趋势 / 低位启动 / 板块状态」三件套**；缓存 + 数据覆盖状态 |
| 板块 RPS | `backend/app/services/sector_rps.py` | `rps20_on` / `board` / `track` | 板块 `pct_chg` 复利 + `rps.mid_rank` |
| 板块→个股 | `backend/app/services/pools.py` | `sector_stock` / `sector_stock_track` | 依赖当日 `sector_member`；**已知负超额** |
| 池内因子 | `backend/app/services/factor_score.py` | `score_series` / `scores` | rev20 / illiq20 / amp20，池内分位 |
| 个股阶段 | `backend/app/services/stock_phase.py` | `load_phase` / `load_phases` | 壳层，判定在 `patterns.classify_phase` |
| 行业宽度 | `backend/app/services/industry_breadth.py` + `jobs/collect_breadth.py` | `above_ma20` 等 | 与 `breadth_daily` 同口径 |
| 恐贪 | `backend/app/services/fear_greed.py` + `jobs/collect_fear_greed.py` | `percentile_score` | 只存原始值，分数现算 |
| 涨跌停规则 | `backend/app/services/limit_rules.py` | `limit_pct` / `board_price` / `is_bse` | 回测与实盘共用（10cm/20cm/30cm、ST） |
| 交易日历 | `backend/app/services/trading_days.py` | `is_trade_day` / `previous_trade_day` / `recent_trade_days` | **唯一入口**（项目约定禁止再写日历 SQL） |

### 2.2 研究与回测层

| 能力 | 文件 | 说明 |
| --- | --- | --- |
| 统一回测内核 | `backend/app/services/research_backtest.py` | `BarMatrix` + `Sample` + `Benchmark`；默认 **T+1 开盘**入场、停牌顺延、开盘涨停买不进、退出跌停标记、`cost_bps` 往返成本、按市场环境分层 |
| 回测 CLI | `scripts/backtest_research.py` | `--strategy rps/trend/pattern/sector`，参数写进报告，可复现 |
| 趋势模板验证 | `scripts/validate_trend.py` | 非重叠留出块、参数敏感性、日期聚类 bootstrap、结论分类 |
| 形态长历史回测 | `scripts/backtest_patterns.py`（136 KB） | 45+ 形态 × 5 年；**收盘入场**，仅横向比较 |
| 因子 IC | `scripts/factor_ic.py` | 横截面 IC / t 值 |
| 数据审计 | `scripts/audit_research_data.py` | `mode=ro` 只读审计，输出覆盖率等级 |
| B0/B1 基础设施 | `docs/b0/*`、`docs/b1/*`、`backend/data/b0_*`、`b1_*` | 数据审计、基线结果、RPS 扩展、板块回补、趋势验证 |

### 2.3 调度（现有盘后链）

`backend/app/jobs/scheduler.py` 15:05 `_run_daily` → `_run_tail` 的执行顺序：

```
collect_kline → backfill_kline → fear_greed → breadth → avg_price → futures_basis
→ erp → valuation → qvix → scan_patterns → scan_rps → backfill_hit_kline
→ push_brief（可延后）→ backfill_hit_dde → scan_dde → collect_board_flow
→ _maybe_remind_calibration → _mark_tail
```

并发的补采链：17:30 / 19:30 / 22:35（`_run_late`，`skip_done` 语义）；22:00 / 22:30 / 08:30 板块成分股；
20:00 告警；23:10 备份；09:15–09:31 竞价；14:50 B1 盘中预览（只推飞书，**不写库**）。

**含义**：趋势引擎的快照任务应挂在 `scan_rps` **之后**、`collect_board_flow` 之前/之后均可，
但必须满足：① 数据未完整不生成正式排名；② 幂等；③ 使用 `collect_guard` 与 `_record_log` 记录。

### 2.4 API 与前端

| 层 | 现状 |
| --- | --- |
| 研究接口 | `backend/app/api/research.py`：`/api/research/workbench`、`/meta`、`/trend`、`/low-start`、`/sector-state`，全部 `Depends(get_research_db)` 只读 |
| 池接口 | `backend/app/api/pools.py`：`/rps`、`/first-board`、`/sector-rps`、`/sector-stock` 及各自 `track` |
| 前端路由 | `frontend/src/App.tsx:85-326`，**不存在 `/research/trend`** |
| 现有研究页 | `frontend/src/pages/Research.tsx`（三 Tab：趋势/低位/板块），调用 `researchApi.trend/lowStart/sectorState` |
| 前端组件 | `Panel`、`LoadState`、`Segmented`、`SortTh`、`StockLink`、`EChart`、`KLineChart`（props `bars/height/keyLevels`） |
| 图表 | ECharts 6（按需注册），无 lightweight-charts |
| 样式 | Tailwind v4 + `index.css` 的 `.panel` / `.grid-table`；无 CSS Modules |
| API 契约 | `frontend/src/api/client.ts` + `types.ts`；CI 跑 `scripts/check_api_contract.py` |

**已发现的前端缺陷（既有，非本次引入）**：`Research.tsx` 未使用 `Layout`，且根节点用了未定义的
`page-stack` / `data-table` 类名（`Research.tsx:240,360,423,483`）。新增 `/research/trend` 必须走 `Layout`。

### 2.5 测试与 CI

| 项 | 现状 |
| --- | --- |
| 后端测试 | `tests/` 共 142 个 `.py` 文件；与本次相关：`test_research_screen.py`、`test_research_backtest.py`、`test_scan_rps.py`、`test_trend_template.py`、`test_pools_trend.py`、`test_s5_pit_times.py`、`test_backtest_*` |
| 本次基线抽测 | `pytest -q tests/test_research_screen.py tests/test_trend_template.py tests/test_scan_rps.py tests/test_research_backtest.py` → **58 passed in 10.41s** |
| 最近全量 | B1 报告记录 752 passed（2026-11-09）；本次未重跑全量 |
| 前端 | vitest + jsdom，20 个测试文件（含 `Research.test.tsx`）；`npm run build/lint/test` 在 CI |
| 契约 | `scripts/check_api_contract.py` 挂在后端 CI job |

---

## 3. 服务调用关系

```
[外部数据源] iFinD(MCP) / 开盘红 / akshare / 腾讯 / 东财
        │
        ▼
[jobs] collect_*.py  ──────► SQLite(research db, 生产只读挂载)
        │                        ▲
        │ scan_rps / scan_patterns / collect_board_flow
        ▼
[services 指标层]
  patterns.build_bars ──► trend.passes / research_screen._trend_rows / pools._filter_trend
  rps.ret / mid_rank ───► scan_rps（落库） / sector_rps / research_screen._ret
  factor_score ─────────► pools.rps_board(order=factor)
        │
        ▼
[services 研究层]
  research_screen.{trend_rows, low_start_rows, sector_states}
        │            └─ data_revision.status（缓存门禁）
        ▼
[services 回测层]
  research_backtest.run_signals ← signals_from_{rps,pattern,trend_template,sector_stock}
        │                            ▲
        │ scripts/backtest_research / validate_trend / backtest_patterns
        ▼
[api] research.py / pools.py / sector.py / market.py / patterns.py  （Depends(get_research_db)）
        │
        ▼
[frontend] Research.tsx / Pools.tsx / PostMarketWorkbench.tsx / Sectors.tsx
```

---

## 4. 用户点名六类问题核查

> 以下每条都给文件与行号；P0 = 进入 M1 前必须修，P1 = 进入对应模型阶段前必须修，P2 = 记录并标注。

### 4.1 重复指标计算

| # | 指标 | 重复实现 | 影响 |
| --- | --- | --- | --- |
| F3 | **距高点 / 回撤** | ① `research_screen._trend_rows` 用 `close[-1] / max(bars.high[-60:]) - 1`（`research_screen.py:485`）；② `pools._off_high_pct` 用 `pct_chg` 复利净值 / 最高净值（`pools.py:596-632`） | 前者用**最高价**、后者用**最高收盘**；前者是不复权 close 比复权 bars 的最高价，除权日会失真。**P0** |
| F10 | **量能比** | ① `patterns._volume_ratio`（`patterns.py:1425`，分母不含当日）；② `MA_VOLUME_*` 注释明确「分母含当日」（`patterns.py:126-131`）；③ `research_screen._amount_ratio`（20 日**成交额**、不含当日，`research_screen.py:361`）；④ `api/market.py` 指数量比 | 同名「量比」四种分母。趋势引擎必须指定唯一口径并写进 `calculation_version`。**P1** |
| F17 | **均线/斜率** | ① `trend.passes` 内 `np.mean(close[i-49:i+1])`；② `research_screen._ma/_slope`（`research_screen.py:333-345`，斜率 = 5 日 MA 变化率）；③ `patterns._ma_series/_align`；④ `pools` 与 `backtest_patterns` 各自的窗口切片 | 公式本身一致，但**归一化方式**不同（有的用 %，有的用绝对差）。M1 建 `indicators.py` 统一提供 `ma/slope/normalized_slope`。**P1** |
| F16 | **ATR** | 只有 `patterns._low_atr_growth` 内部现算 `ATR14/收盘`（`patterns.py:5905-5916`），无通用 `atr()` | 任务书要求 ATR14/20 与 ATR 标准化回撤。**P2，M1 补纯函数** |

### 4.2 同一指标不同口径

| # | 指标 | 差异 | 证据 |
| --- | --- | --- | --- |
| F4 | **板块 RPS** | `sector_rps.rps20_on` 用 `rps.mid_rank`（并列同分）；`research_screen._sector_states` 用 `(index+0.5)/n*100`（并列按输入顺序给不同分） | `sector_rps.py:162-178` vs `research_screen.py:1047-1054`。**P0**：同一「板块 RPS20」在选股池页与研究页可以不同 |
| F5 | **板块区间收益** | 板块 `ret20/ret60` 复利时 `if pct is not None: value *=`，**缺日静默跳过、不校验覆盖率**；个股 RPS 有 90% 覆盖闸 | `research_screen.py:1036-1066` vs `rps.ret`（`rps.py:23-30`）。**P0** |
| F11 | **「趋势」定义** | ① Minervini 250 日模板；② 研究页的 `close>MA20>MA60 + trend_days + 斜率`；③ 任务书 Model A/B | 三套语义不同，不能合并；但必须共用底层指标，并对每条输出 `model_version`。**P1** |
| F18 | **分位函数** | `rps.mid_rank`（中位秩）vs `rps.percentiles`（端点 `rank/(n-1)`，池内排序用） | `rps.py:42-76`；已在注释中区分，新模型不得混用。**P2（文档级）** |
| F19 | **「资金净流入」** | 任务是要求「不得把单日成交额增加解释为净流入」；现有 `stock_dde.net_inflow` 是 iFinD 逐股口径，`sector_daily.net_inflow` 是「成分股 × 逐股净流入」自算，只有 724 行有值 | `models.py:296-303`、`jobs/collect_board_flow.py`。**P2：趋势模型默认不用资金流字段** |

### 4.3 未来数据风险（PIT）

| # | 风险点 | 现状 | 判定 |
| --- | --- | --- | --- |
| F7 | 交易日历预置未来 | `trade_calendar` 到 2026-12-31；`sector_rps._forward_returns` 已用 `data_end` 封顶（`sector_rps.py:250-262`），`research_backtest._find_entry/_find_exit` 用矩阵末日封顶 | **已有正确范式，新模块必须照做** |
| F6 | 行业归属非历史 | `StockBasic.industry` 是**当前**行业；`research_screen._industries`（`:306`）与 `pools._industry_top` 用它给历史日分组 | **前视**。P1：若做行业中性化，必须标 `industry_asof=today` 或改用当日可得字段 |
| — | 板块成分 | 历史用 `SectorMember.trade_date == day` 当日快照（`research_screen._boards`） | **正确**；但不能把今天的成分套到去年 |
| — | 股票池 | `scan_rps` 用当前 `stock_universe` 卡覆盖率，历史回补用 `estimate_pool_count`（估计值） | 已缓解；报告必须标注「估计池」 |
| — | ST 判定 | 研究层用**当日** `stock_daily.name`（`research_screen.py:454`，`research_backtest` 用入场日 name） | **正确** |
| — | 幸存者偏差 | `history.db` 由当前代码池回补，0 只退市股（B0 审计） | 结论只能标「现存股票样本研究」 |
| — | 形态命中历史 | `pattern_hit` 只有 26 个命中日，且是当日扫描写入 | 不能当连续历史信号源；回测须重扫 |

### 4.4 缺失值静默填充

| 位置 | 行为 | 判定 |
| --- | --- | --- |
| `research_screen` 主体 | 返回 `None` + `missing[]`，不填 0 | **正确，作为新模块范式** |
| `scan_rps` | 不可算单元格显式置 NULL，保留主键行 | **正确** |
| `_sector_states` 板块收益 | 缺日跳过，不报缺失 | **F5，P0** |
| `factor_score` | 缺失因子按 0 分位参与 | 有注释；趋势评分**不得**照搬，必须显式标记缺失并拒绝给高分 |
| `breadth_daily` 空表 | 读取侧返回 `None`（`api/market.py:499-523`） | 页面显示 `—`；但市场环境需要它 → M4 前必须重算 |
| `sector_daily.up_count/down_count` | 全 NULL | 板块广度只能走 `sector_member` |

### 4.5 交易日不一致

| 现象 | 证据 | 处理要求 |
| --- | --- | --- |
| 个股 10-09 / RPS 10-21 / 指数与板块 09-24 | 1.4 节实测 | API 必须返回 `requested_date / actual_data_date / fallback`（`sector_states` 已有范式，`research_screen.py:958-1010`） |
| `history.db` 无交易日历 | `research_backtest.load_matrix` 退化到行情日期并集并告警（`:199-222`） | 长历史回测报告必须写明「日历来自行情并集」 |
| 指数日历与个股日历可能不同 | `_regime_map` 用 `index_daily` 行序，`BarMatrix` 用 `trade_calendar` | 市场环境按**指数自己的交易日**判定，再对齐到个股日 |
| 研究缓存键 | `data_revision` 的域版本；b2/b1 **没有 `data_revision` 表** | **F1，P0**：缓存实际处于关闭状态，每次请求全市场重算 |

### 4.6 回测与实盘口径

| 项 | 现状 | 判定 |
| --- | --- | --- |
| 入场 | `research_backtest` 默认 `next_open`（T 日收盘后出信号、T+1 开盘成交） | **符合任务书**；`entry="close"` 仅理论对照 |
| 不可成交 | 开盘涨停（`_entry_limit_up`）、停牌顺延（`max_entry_delay=5`）、退出跌停标记 | **已实现** |
| 成本 | `cost_bps` 一次往返总成本 | **已实现**；趋势回测必须显式给非 0 值并单列 |
| 基准 | 同 (入场日, 退出日) 全市场等权，剔除入场涨停/退出跌停 | **已实现**；可用 `index:000001.SH` |
| RPS 口径 | 线上与回测共用 `rps.py` | **已统一**（2026-10-24 修） |
| 趋势模板 | 线上 `pools._filter_trend` 与回测 `signals_from_trend_template` 共用 `trend.evaluate` | **已统一** |
| 形态回测 | `backtest_patterns` 用收盘入场、当前代码池、最新名称判 ST | **与实盘不一致**，文档已声明仅横向比较；趋势引擎**不得**复用它当收益结论 |
| 市场环境分层 | 只有 `research_backtest._regime_map`（进攻/防守/震荡），且 `index_daily` 止 09-24 | 任务书 BULL/SIDEWAYS/BEAR/EXTREME_RISK **尚不存在**；M4 新建并在回测中复用同一函数 |

---

## 5. 既有研究结论对设计的直接约束

> 这些结论来自项目内已归档报告，**新模型不能无视**。

| 结论 | 数值 | 出处 | 对任务书的影响 |
| --- | --- | --- | --- |
| RPS 单独不是买点 | 5 年：10 日 **−0.84%**；近一年 +0.92% | `docs/回测_RPS与趋势模板_20261024.md` §2、§8.2 | Model A/B 的 RPS 只能做**排序权重**，不能做硬过滤（任务书 4.4 的「RPS20 进入前 20%」需改成可关） |
| 趋势模板有条件成立 | 5/10/20/40/60 日超额 **+0.78/+1.57/+2.51/+3.38/+3.98**，但非重叠块 60 日 −0.96/+17.77/−18.16 | `docs/b1/2026-11-09-b1-final-report.md` §3 | 可作为 Model B 的结构基线，但必须做行业中性 + purged walk-forward 才能进页面 |
| 趋势模板收益靠尾部 | 前 5% 样本贡献 142.1%，剔除后 60 日 **−2.08%** | 同上 | 评分必须同时输出「集中度/尾部依赖」提示，不能只给平均值 |
| 板块叠加是负的 | B（板块 RPS20≥90）20 日超额 **−18.74** vs A（仅趋势）**−11.00**；C 组 −19.69 | 同上 §4 | 任务书 4.6 的「板块环境 10 分」、5.4 的「板块持续性 10 分」**默认值必须经回测决定**，首版建议 ≤5 分且可关 |
| 板块→个股负 IC | IC −0.042（t=−1.7）、强势板块×突破超额 −0.30% | `docs/回测_板块到个股_20261025.md` | 板块只做「背景/风险变量」，不做硬过滤 |
| 短/长窗口方向相反 | 「双高」近一年 +3.1~4.1%，5 年 −0.4~−1.4% | `docs/回测_RPS与趋势模板_20261024.md` §8.3 | 所有阈值必须在**两个样本期**同时报告 |
| 数据只能算「现存股票样本」 | `history.db` 0 只退市股 | `docs/b0/2026-11-08-b0-data-audit.md` §3.1 | 回测报告必须带此声明，禁止外推 |

---

## 6. 模块映射：任务书建议目录 → 复用 / 新建

| 任务书模块 | 决策 | 落点 |
| --- | --- | --- |
| `config.py` | 新建（薄） | `backend/app/services/trend_engine/config.py`：阈值、权重、版本号；**全部可被回测覆盖** |
| `indicators.py` | 新建 | MA/slope/ATR/回撤/量能比纯函数；**只消费 `patterns.Bars`，不重写 `build_bars`** |
| `trend_structure.py` | 新建 | MA 多头/空头/收敛、拐头、MA20→MA60 距离；复用 `indicators` |
| `momentum.py` | 新建（薄） | RET5~250 用 `rps.ret`；RPS 直接读 `rps_daily`（不重算） |
| `volume_price.py` | 新建 | 放量突破/缩量回调/量价背离；口径固定并写入版本 |
| `sector_strength.py` | **扩展** | 在 `sector_rps.py` 上补 60 日、板块内上涨比例、强势股数、排名变化；先修 F4/F5 |
| `market_regime.py` | 新建 | BULL/SIDEWAYS/BEAR/EXTREME_RISK；数据源 = `breadth_daily`（需重算）+ `index_daily` + `market_sentiment` + `fear_greed_daily`；**回测复用同一函数** |
| `breakout_model.py` | 新建 | 复用 `patterns` 的平台突破/海龟/放量突破判定思路；平台高点窗口**必须排除当日** |
| `continuation_model.py` | 新建 | 复用 `trend.passes` 的结构部分 + 缩量回踩；ATR 标准化回撤 |
| `risk_engine.py` | 新建 | NORMAL/WARNING/BROKEN；多条件确认，禁止单日跌破即 BROKEN |
| `scoring.py` | 新建 | 分项得分 + 总分 + 缺失惩罚；**两个模型各自独立总分** |
| `screening.py` | 新建 | 硬过滤（ST/停牌/流动性/数据质量）+ 排序 + TopN；不强行补足数量 |
| `snapshots.py` | 新建 | 每日快照写入（幂等、不可无痕覆盖） |
| `explanation.py` | 新建 | 结构化入选/排除原因；参考 `stock_research_context.py` 的「已知/未知」分离思路 |
| — | **不建** | RPS 计算、前复权 K 线、涨跌停规则、交易日历、回测内核、调度器、缓存门禁 |

**目录建议**：`backend/app/services/trend_engine/`（不新建顶层包），与现有 `services/` 同级。

---

## 7. 数据库方案

### 7.1 复用（禁止新建同义表）

`stock_daily`、`rps_daily`、`sector_daily`、`sector_member`、`pattern_hit`、`index_daily`、
`market_sentiment`、`fear_greed_daily`、`breadth_daily`、`limit_pool`、`stock_basic`、`trade_calendar`。

### 7.2 新增 5 张表（全部只由后台任务写入）

| 表 | 主键 | 关键列 | 写入语义 |
| --- | --- | --- | --- |
| `trend_model_versions` | `model_version` | `model_name`, `params_json`, `calculation_version`, `created_at`, `status` | 只增不改 |
| `trend_daily_scores` | `(trade_date, code, model_name, model_version)` | 总分 + 5 个分项、RPS20/60/120/250、MA20/60/120、ATR14、ret20/60、回撤、成交额、`data_quality_status` | 幂等 upsert；**同一 `(trade_date, model_version)` 重跑结果一致** |
| `trend_signal_snapshots` | `(trade_date, model_name, code)` | `rank`, `score`, `pool`, `sector_code`, `market_regime`, `reasons_json`, `exclusions_json`, `created_at` | **历史快照不可覆盖**；同日重跑只允许同版本覆盖，换版本新增行 |
| `trend_pool_members` | `(trade_date, pool, code)` | `pool`（start/continuation/watch/warning/broken）, `score`, `entered_at`, `exit_reason` | 每日全量替换当天、历史保留 |
| `trend_backtest_results` | `(run_id)` | `strategy`, `params_json`, `start`, `end`, `horizons_json`, `metrics_json`, `data_version`, `created_at` | 只增不改；`run_id` 由参数哈希生成 |

```sql
-- 示例：快照表的不可覆盖约束
CREATE TABLE trend_signal_snapshots (
  trade_date        DATE NOT NULL,
  model_name        TEXT NOT NULL,
  code              TEXT NOT NULL,
  model_version     TEXT NOT NULL,
  calculation_version TEXT NOT NULL,
  created_at        DATETIME NOT NULL,
  PRIMARY KEY (trade_date, model_name, code, model_version)
);
```

### 7.3 数据修订域（缓存）

`data_revision.DOMAINS` 当前覆盖 `stock_daily/stock_basic/stock_universe/trade_calendar/rps/sector_daily/sector_member/backfill_manifest`
（`backend/app/services/data_revision.py:30-41`）。新增 5 张表后必须：

1. 在 `DOMAINS` 增加 `trend` 域；
2. 用 `scripts/install_data_revision.py --db <isolated> --strict` 在**隔离研究库**安装并验收 24+ 触发器；
3. 生产安装需单独授权。

**F1（P0）**：b2/b1 研究库**没有 `data_revision` 表** → `status()` 返回 `not_installed`
→ `research_screen._cached` 每次请求都走 `compute()`。新增趋势页是全市场扫描，若不先解决，
每个请求都会重算 5,000 只票。

---

## 8. API 与前端设计约束

| 端点 | 复用/新建 | 约束 |
| --- | --- | --- |
| `GET /api/research/trend/overview` | 新建 | 返回市场环境 + 板块 Top + 双模型计数 + `data_status` |
| `GET /api/research/trend/start` | 新建 | `model=A`，Top20，含分项与 reasons |
| `GET /api/research/trend/continuation` | 新建 | `model=B`，Top20 |
| `GET /api/research/trend/warnings` | 新建 | NORMAL/WARNING/BROKEN 列表 |
| `GET /api/research/trend/stock/{code}` | 新建 | K 线 + MA20/60/120 + RPS 曲线 + 入选历史（读快照） |
| `GET /api/research/trend/sectors` | 扩展 | 复用 `sector_states`，补 60 日与排名变化 |
| `GET /api/research/trend/history` | 新建 | 读 `trend_signal_snapshots`，只读 |

**统一要求**：
- 全部 `Depends(get_research_db)` + `current_user`；**普通用户请求不得写库**（`require_research_writable` 现有语义，`api/deps.py:15-26`）；
- 响应必须含 `requested_date/actual_data_date/fallback/data_status/coverage/model_version/calculation_version`；
- 新增函数与类型必须同步 `frontend/src/api/client.ts` 与 `frontend/src/api/types.ts`；
- 参数上下限写入 `scripts/check_api_contract.py` 的 `CHECKS`；
- 前端新页面 `/research/trend` 使用 `Layout`，复用 `Panel/Segmented/EChart/KLineChart/StockLink`。

---

## 9. 开发计划（M1–M8）

> 每个阶段完成即：`git diff` 检查 → 定向测试 → 证据落盘 → 提交 → 报告 → push。
> 分支：`feat/trend-engine-v1`，从当前 HEAD `f921aed` 切出（不从 master）。

| 阶段 | 交付 | 验收门禁 | 依赖 |
| --- | --- | --- | --- |
| **M1 指标层** | `trend_engine/{config,indicators,trend_structure,momentum,volume_price}.py` + 单测 | 纯函数、无 DB；与 `rps.ret` / `trend.passes` 交叉一致性测试；ATR/回撤/量能比各只有一份实现 | 修 F3/F10/F16/F17 |
| **M2 启动模型** | `breakout_model.py` + `scoring.py`（Model A）+ 单测（平台突破排除当日、放量确认、排除条件） | 历史抽样日可解释；PIT 测试（只用 ≤T 数据） | M1 |
| **M3 延续模型** | `continuation_model.py` + Model B 评分 + 单测 | MA60 向下例外单独标记；ATR 标准化回撤 | M1 |
| **M4 板块与市场** | 修 F4/F5；`sector_strength.py`、`market_regime.py`；重算 `breadth_daily` | 板块 RPS 与 `sector_rps.board` 完全同分；环境分类在 09-24 前后均有值或显式 `unknown` | F4/F5；需要补 `index_daily`/`breadth_daily` |
| **M5 风险引擎** | `risk_engine.py` + 单测（NORMAL/WARNING/BROKEN 多条件） | 单日跌破不得直接 BROKEN；输出失效参考价与复核日 | M1–M3 |
| **M6 统一回测** | 扩展 `research_backtest` 的信号提供器（Model A/B/组合）+ `scripts/backtest_trend_engine.py` | `next_open` + 成本；训练/验证/样本外 + walk-forward；结果写 `trend_backtest_results` | M2–M5；`b1_research.db` / `history.db` |
| **M7 盘后运行** | `jobs/trend_engine.py` + 调度挂载 + 幂等测试 | 同交易日重跑快照不增行；数据不完整不生成正式排名；`data_revision` 已安装 | M6；F1 |
| **M8 前端** | `/research/trend` 页面 + `client.ts/types.ts` + 契约登记 | 分项可解释；fallback 与 `data_status` 可见；`npm run build/lint/test` | M7 |

**明确不在本轮**：盘中实时扫描、组合资金/仓位模拟、自动下单、AI 预测。

---

## 10. 阻断项与需要确认的决策

| # | 阻断/决策 | 现状 | 建议 |
| --- | --- | --- | --- |
| B1 | 研究缓存默认关闭（b2/b1 无 `data_revision`） | 每请求全市场重算 | M1 先在隔离库安装修订水位；生产安装另行授权 |
| B2 | 板块权重与既有负证据冲突 | B1 报告 B/C 组显著更差 | 首版权重 ≤5 分且可关；用 M6 回测决定默认值 |
| B3 | RPS 阈值当硬过滤与既有负证据冲突 | 5 年 RPS 单独负超额 | RPS 只做排序/分项，不做硬性 `RPS60>=80` 的一刀切 |
| B4 | 生产只有约 34 天 RPS 历史 | 不足以做 walk-forward | 历史验证只在 `b1_research.db`（506 天）与 `history.db`（1,230 天、仅现存股） |
| B5 | `index_daily` / `sector_daily` / 情绪表止于 2026-09-24 | 比个股末日落后 9 个交易日 | M4 前补采；未补齐前环境判定必须返回 `actual_data_date` + `fallback` |
| B6 | `breadth_daily` 为空 | 市场环境缺核心输入 | M4 用 `jobs/collect_breadth.py` 重算，并在报告标注窗口 |
| B7 | 新增表需要生产写入授权 | 研究库只读 | 先在隔离库演练 → 授权 → `install_data_revision.py --strict` → 上线 |
| B8 | 分支基线 | master 落后 174 个提交 | 从 `f921aed` 切 `feat/trend-engine-v1`，不合并 master |
| B9 | 退市股与历史 ST 状态缺失 | 幸存者偏差 | 所有回测结论统一标注「现存股票样本」，不包装成策略收益 |

---

## 11. 本次审计执行记录（只读）

```text
git status / branch -a / log / rev-list --left-right --count     # 分支与差异
Get-ChildItem backend, scripts, tests, docs, reports             # 文件清单
Select-String models.py / scheduler.py / main.py / config.py     # 表、调度、路由、配置
Read trend.py rps.py sector_rps.py research_screen.py
     research_backtest.py scan_rps.py pools.py factor_score.py
     stock_phase.py stock_research_context.py data_revision.py
     api/research.py api/deps.py patterns.py(节选)
Read docs/策略.md docs/回测_RPS与趋势模板_20261024.md
     docs/b0/2026-11-08-b0-data-audit.md docs/b1/2026-11-09-b1-final-report.md
     docs/b3/2026-11-11-b3-ths-hq-and-b1-audit.md
     reports/s6_12/08-production-upgrade-report.md
sqlite3 mode=ro（b2/b1/fupan/history/forward）                  # 行数、日期范围、混版本行
pytest -q tests/test_research_screen.py tests/test_trend_template.py
         tests/test_scan_rps.py tests/test_research_backtest.py  # 58 passed in 10.41s
```

**未执行**：未写任何数据库、未建表、未改代码、未改调度、未部署、未 push、未合并 master。

---

## 12. 审计结论

1. **不需要重建**：RPS、前复权 K 线、形态、板块日线、回测内核、调度、只读边界均已存在且经过测试，
   趋势引擎应作为 `services/trend_engine/` 扩展层，复用这些底层能力。
2. **必须先修 5 个 P0 口径问题**：研究缓存不可用、RPS 混版本/孤儿日、距高点两套、板块 RPS 两套、
   板块收益缺日静默跳过。否则新模型会继承「同一指标不同结论」的问题。
3. **两处任务书设定与项目证据冲突**：RPS 硬过滤、板块加分。建议首版降为低权重 + 可关闭，
   由 M6 统一回测在两个样本期上决定默认值。
4. **数据窗口决定验证范围**：生产只有约 34 天 RPS 历史，历史验证必须在隔离研究库完成；
   指数/板块/情绪表落后个股 9 个交易日，M4 前必须补采或显式 fallback。
5. **下一步**：批准本审计后切 `feat/trend-engine-v1`，进入 M1（指标层），
   先交付纯函数与单元测试，不碰数据库写入与前端。
