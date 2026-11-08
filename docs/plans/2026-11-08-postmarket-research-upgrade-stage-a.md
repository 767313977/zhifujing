# 致富经盘后研究升级 · 阶段 A 全局审查与设计

> 日期：2026-11-08
> 仓库：`https://github.com/767313977/zhifujing`
> 审查基线：`44f0912d9f754e253b06e1296cb1f8b381f158a0`（工作区干净）
> 范围：只做审查与设计，**未修改业务代码、未连接生产、未部署、未改数据库**
> 产品定位：趋势股、低位启动、板块轮动、中短线选股（5~60 个交易日），盘后数据为主

---

## 0. 结论摘要

1. **基础设施比上一版任务书描述的更完整**：交易日历、前复权 K 线构造、全市场形态扫描、RPS、板块 RPS、板块轮动矩阵、形态跟踪回测、首板次日验证、交易计划/日志/自选都已存在，且口径写在代码注释与 `docs/` 里。
2. **新 P0 五项没有一项是「已完整实现」**，但大部分所需原料已有：
   - 板块轮动：缺 10/60 日窗口、逐板块排名序列、成交额比值、内部广度、连续强势天数、确定性状态分类。
   - 趋势股：缺 MA20/60/120 特征层、RPS20/60、相对板块/指数超额、趋势持续天数、可配置阈值与「未满足条件」解释。
   - 低位启动：现有形态只覆盖「平台突破 / 箱体 / 均线粘合」等布尔判定，缺「历史区间位置 + 平台长度 + 波动收敛 + 突破基准 + RPS 改善 + 板块同步」的组合状态机。
   - 板块个股共振：现有 `sector-stock` 是「前 N 强板块 × 每板块前 N 只」，一只票多板块时只保留首个板块，且只有 1 日跟踪；不满足「分别保留板块强度 / 个股强度 / 共同改善」。
   - 策略验证：已有回测引擎的雏形（`pattern_track`、`backtest_patterns.py`、`first_board_track`、`sector_rps.track`），但收益口径、持有期、分布、MAE、市场环境、样本外、费用滑点、幸存者偏差修正都不统一。
3. **最重要的产品风险不是「做不出来」，而是「做出来没有效」**：现有回测（`docs/回测_板块RPS_20261024.md`、`docs/回测_板块到个股_20261025.md`、`docs/回测_RPS与趋势模板_20261024.md`）显示：
   - 板块 RPS 动量在近 200 日为正（行业口径 20 日 +1.26pp），但**近 60 日为负**（−1.5 ~ −2.8pp）；
   - 「板块强 → 个股强」近 59 日 IC 为 **−0.046**，强势板块×突破超额 **−0.30%**；
   - RPS 单独选股在 5 年长历史没有稳定超额，只有「双高 × 突破」在最近一年有效。
   因此本设计把 **P0-5 的验证内核提前到第一阶段**：先有可复现、无未来函数、可扣费的统一回测，再让 P0-1~P0-4 的页面指标上线。否则会重演「先做页面、后补验证」的老问题。
4. **数据是当前最大瓶颈**：本地库是快照（个股到 2026-10-09、板块到 2026-09-24），`sector_member` 只有 5 天、`rps_daily` 只有 2 天、板块资金流只有 2 天。P0-1 的广度、P0-4 的共振历史、P0-5 的样本外验证都依赖这些表的持续积累或一次性回补。
5. **明确无需开发**：交易日历处理、前复权 K 线（`build_bars`）、同日全市场等权基准矩阵（`pattern_track._matrix`）、板块 RPS 基础口径（`services/rps.py`）、板块轮动矩阵、板块→个股跳转、自选/交易计划/日志、数据库自动加列与备份。

---

## 1. 审查基线

### 1.1 Git 与工程状态

| 项 | 事实 |
| --- | --- |
| HEAD | `44f0912 fix(audit-20261108-r4): 待注销三态、跨标签页多任务锁与过期 authMe 失效` |
| 工作区 | `git status --short` 无输出（干净） |
| 后端 | Python + FastAPI + SQLAlchemy + SQLite；未使用 Alembic；`db.init_db()` 自动补可空列与索引 |
| 前端 | React 19 + TypeScript + Vite + ECharts + oxlint + vitest |
| 测试基线 | 本次只跑了 `tests/test_pools_track.py tests/test_sector_stock.py`：**9 passed in 0.53s**；全量 84 个后端测试文件未在阶段 A 执行 |
| 数据库 | 本地 `backend/data/fupan.db` 489.7MB；`backend/data/history.db` 829.5MB（只有 `stock_daily`） |

### 1.2 前端命令（`frontend/package.json`）

```bash
npm run test      # vitest run
npm run lint      # oxlint
npm run build     # tsc -b && vite build
```

后端：`.venv\Scripts\python.exe -m pytest -q`（Windows）。

---

## 2. 现有能力盘点

### 2.1 数据与模型（`backend/app/models.py`）

| 表 | 关键字段 | 现状 |
| --- | --- | --- |
| `trade_calendar` | `trade_date` | 1990-12-19 ~ 2026-12-31（含未来预置），全站日期口径的唯一来源 |
| `stock_daily` | OHLC、`pct_chg`、`volume`、`amount`、`turnover`、`name` | 不复权价 + 真实涨跌幅；**逐日 `name` 非空率 100%**（可还原历史 ST 状态） |
| `stock_basic` | `name`、`industry`、`total_mv`、`free_float_shares`、`pe_forecast` | 行业为同花顺三级路径；市值是快照日，读取时按股价缩放 |
| `sector_daily` | `strength`、`pct_chg`、`amount`、`net_inflow`、`up_count`、`down_count`、`member_count`、`leader_name`、`leader_pct_chg` | `strength/pct_chg/amount` 100% 有值；`up_count/down_count` **全为 NULL**；`net_inflow/member_count` 仅 724/94771 行有值 |
| `sector_basic` | 代码、名称、`taxonomy` | 区分 `kph_selected` / `kph_industry`，**两套口径不可混比** |
| `sector_member` | 日期、板块、个股、涨跌、成交额、换手 | 模型明确「打开板块现取 + 22:00 预取」，**不是逐日回补** |
| `stock_concept` | 日期、个股、概念 | 只覆盖当日涨停股（开盘红涨停天梯） |
| `limit_pool` / `limit_reason` | 涨停/跌停/炸板、连板、封单、首封、涨停原因 | 保留但本轮不优先强化 |
| `rps_daily` | `ret50/120/250`、`rps50/120/250` | **没有 20/60 日**；历史只有逐日扫描过的日期 |
| `pattern_hit` | 日期、代码、形态、评分、关键位、明细 | 只存命中行；扫描只覆盖主板+创业板+科创板（不含北交所） |
| `market_sentiment` | 涨停数、封板率、连板高度、涨跌家数 | 255 天 |
| `index_daily` | 6 个指数 OHLC/涨跌/成交额/宽度 | 2025-09-08 起 |
| `pattern` 相关 | — | 注册表 `PATTERNS` 共 40+ 个形态，定义在 `services/patterns.py:6053` |

### 2.2 计算与回测服务

| 能力 | 位置 | 口径要点 |
| --- | --- | --- |
| 前复权 K 线 | `services/patterns.py:1107 build_bars` | 用 `pct_chg` 复利重建收盘，OHLC 按当日比例换算，成交量同步缩放；**除权问题已解决，必须复用** |
| 可用 K 线/断裂截断 | `usable_bar`（1094）、`stock_phase.trim_contiguous_tail`（115） | 停牌残行剔除；非连续交易日截断，宁缺毋滥 |
| 个股 RPS | `services/rps.py`、`jobs/scan_rps.py` | `pct_chg` 复利、按交易日截窗、90% 覆盖、中位秩；窗口仅 50/120/250 |
| 板块 RPS | `services/sector_rps.py:44` | 窗口仅 (5, 20)；另一套「开盘红强度」`sector_daily.strength` 不能与 RPS 混排 |
| 板块轮动矩阵 | `api/sector.py:627 rotation` | 每列独立按指标排序取前 N；**没有逐板块排名时间序列** |
| 板块资金流 | `api/sector.py:838/922/1040` | 依赖 `sector_daily.net_inflow`/`sector_fund_flow`，历史极短 |
| 趋势模板 | `services/trend.py:43 passes / 72 evaluate / 86 load_bars` | Minervini 8 条，250 根日线；回测与 `/pools/rps?trend_template` 共用 |
| 阶段判定 | `services/patterns.py:2919 classify_phase` | 短线 9 档（启动/明天盯/休息中/走弱…），**不是 5~60 日趋势阶段** |
| 量价因子 | `services/factor_score.py` | 20 日反转、非流动性、低振幅，池内分位加权；只用于排序，不是收益预测 |
| 形态跟踪 | `services/pattern_track.py` | 筛选日收盘 → 第 n 日收盘复利；基准=同日全市场等权；`_matrix` 一次取数复用；`scorecard` 1/3/5/10 日超额 |
| 形态回测脚本 | `scripts/backtest_patterns.py` | 支持 `--entry next_open`、`--skip-unbuyable`、`--since`、`--history`、20 日去重；**已知幸存者/ST 偏差** |
| 首板跟踪 | `services/pools.py:928 first_board_track` | 次日开盘溢价、开盘→收盘、一字板买不进、无行情/待验证分状态 |
| 板块个股跟踪 | `services/pools.py:1509 sector_stock_track` | 仅 1 日收益；复用 `pattern_track._curves` 的矩阵与基准 |
| 板块 RPS 延续性 | `services/sector_rps.py:296 track` | 5/20 日前瞻，板块指数收益，等权基准；有数据末端与缺行守卫 |

### 2.3 前端现状

| 页面 | 现有能力 | 缺口 |
| --- | --- | --- |
| `Sectors.tsx` | 板块排行、精选/行业切换、轮动矩阵（强度/涨幅/成交额，5~60 日）、走势图、多板块对比、资金流当日/多日矩阵；URL 保存 `view/taxonomy/date/code/rot/rotdays/ladder/curve/flow/flowdays` | 无板块状态、无排名变化曲线、无成交额比值、无广度、无「板块内核心股强弱变化」 |
| `Pools.tsx` | 四个面板：首板一进二 / RPS / 板块 RPS / 板块→个股；已有「板块强≠个股强」负超额提示 | 无趋势池、无低位启动池、无共振池；**无 URL 状态**；仅板块 RPS 面板可点板块看成分 |
| `Patterns.tsx` / `PatternTrack.tsx` | 形态目录、命中筛选、阶段筛选、复制代码、加计划、跟踪/成绩单/到线率 | 无中位收益/分布图；无 40/60 日；无市场环境拆分；K 线不画均线 |
| `Dashboard.tsx` | 结论条、指数、恐贪、情绪、板块热力、涨停池、形态节选、自选、笔记 | 无「候选池 / 明日计划」区；只有 8 行形态节选；日期不入 URL |
| `StockDetail.tsx` | K 线、阶段、题材、资金、公告、风险、资金流 | 无 MA20/60/120 显示；无趋势/低位启动状态 |
| 全局 | 个股详情 ← → 浏览（sessionStorage `stock-nav`） | 跳转不带来源页/筛选条件；全站无 CSV 导出 |

---

## 3. 新 P0 五项功能差距表

状态定义：
- ✅ 已实现，无需修改
- ⚠️ 已实现，但有明确缺陷
- 🟡 部分实现，可扩展
- 🟢 尚未实现，现有数据支持
- 🔴 尚未实现，且缺少必要数据

### P0-1 板块轮动研究系统

| 需求 | 状态 | 依据 / 缺口 |
| --- | --- | --- |
| 5/10/20/60 日相对强度 | 🟡 | `sector_rps.WINDOWS=(5,20)`；需扩到 10/60，并写进接口/前端 |
| 历史排名及排名变化 | 🟡 | `/sector/rotation` 只给每日前 N 行，没有逐板块排名序列与 `rank_change` |
| 成交额相对近期均值 | 🟢 | `sector_daily.amount` 100% 有值（255 天），可直接算 5/20/60 日均值比 |
| 板块上涨股票占比/内部广度 | 🔴→🟡 | `up_count/down_count` 全空；只能从 `sector_member` 逐日算；本地仅 5 天，生产文档称已回补 60 天（需核对） |
| 资金流与价格一致性 | 🔴 | `net_inflow` 仅 2 天有值；`sector_fund_flow` 仅 2 天。**不能回补，只能从上线日起积累** |
| 强势板块持续时间 | 🟢 | 由 `sector_daily` 逐日 RPS/排名序列可算 |
| 新晋强势 / 持续走弱 | 🟡 | 只有涨停题材层面的「新晋/延续/退潮」（`theme_board.py:258`），不是板块层面 |
| 板块内部核心股强弱变化 | 🟡 | `sector_member` 有成分与当日涨跌；`leader_name` 只有名称快照、无代码、无历史 |
| 确定性状态分类 | 🟢 | 需新建状态机；现有 `theme_board` 状态只比较前后两日，不能直接复用 |
| 分类体系不混排 | ✅ | `taxonomy` 已是接口必填/默认参数，`sector_rps` 与 `ranking` 都按口径隔离 |
| 排名上升≠上涨、短期下降≠趋势结束 | 🟡 | 现有文档已强调，但没有状态枚举与解释字段来约束读法 |

**明确缺陷**：`sector_rps._latest_meta` 在当天缺行时会把窗口内最近一次的 `net_inflow/member_count/leader` 当作展示值，但响应里没有 `net_inflow_date`，页面可能把旧资金流读成当天。设计上必须补日期字段或只在 `date==target` 时展示。

### P0-2 趋势股筛选系统

| 需求 | 状态 | 依据 / 缺口 |
| --- | --- | --- |
| MA20/60/120 相对位置 | 🟡 | `ma_bull` 形态内部算了均线，但结果只有布尔与评分；没有逐字段输出 |
| 均线斜率变化 | 🟡 | `ma_bull` 用 20 日斜率判「向上」，未输出 MA20/60/120 的斜率值 |
| 收盘相对中长期均线 | 🟢 | `build_bars` 可直接算；缺接口字段 |
| 20/60/120 日价格趋势 | 🟡 | RPS 有 50/120/250 的区间涨幅，**没有 20/60** |
| 趋势持续时间 | 🟢 | 可由逐日收盘/均线状态累计；缺实现 |
| RPS20/60/120 | 🟡 | 表里只有 50/120/250；需加 20/60 两列并回补 |
| 相对行业/板块超额 | 🟢→🟡 | `stock_basic.industry` 覆盖全 A；`sector_member` 有板块归属但历史不足 |
| 相对主要指数超额 | 🟢 | `index_daily` 6 个指数，255 天 |
| 相对强度改善持续性 | 🟢 | 需 RPS 逐日序列（回补后可用） |
| 成交额相对 20 日均值 | 🟡 | `factor_score` 有 20 日流动性/振幅；形态量比用 5 日；没有通用的 20 日成交额比 |
| 突破时/回调时量能 | 🟡 | `volume_breakout`、`dry_pullback` 是形态布尔值，不是通用特征 |
| 换手率与流动性 | 🟡 | `stock_daily.turnover` 有值；`stock_universe` 用 20 日均成交额 ≥2000 万过滤；但没有前端可调流动性条件 |
| ST/退市/受限股过滤 | 🟡 | `is_st` 只看传入名称；回测用「最后一根名字」判 ST（已知偏差）。**逐日 `stock_daily.name` 可修正 ST 部分**；退市股仍缺 |
| 数据不足 / 流动性过低 / 价格异常 / 复权缺陷 | 🟡 | `usable_bar`、`trim_contiguous_tail`、`pct<=-100` 守卫已有；缺统一风险状态输出 |
| 趋势恶化过滤 | 🟢 | 可由 MA20/60 斜率、距 60 日高点回撤计算 |
| 可配置阈值 | 🟡 | `RPS mode`、`min_rps`、`trend_template` 可调；其余阈值写死在常量 |
| 命中依据 / 不满足条件 | 🟢 | 需新增 `reasons / unmet / missing` 字段 |
| 复用现有形态 | ✅ | `PATTERNS`、`/patterns/hits`、`/pools/rps` 全部复用，不重复实现检测 |

### P0-3 低位启动识别

| 需求 | 状态 | 依据 / 缺口 |
| --- | --- | --- |
| 历史区间相对位置 | 🟢 | `build_bars` 前复权 OHLC 可直接算 N 日高低区间位置 |
| 持续整理平台 | 🟡 | `platform_breakout`、`box_breakout` 有检测，但没有输出平台长度/振幅/收敛度 |
| 历史波动收敛 | 🟡 | `ma_squeeze` 只看均线粘合；没有 ATR20/ATR60 或振幅比 |
| 突破整理区间 | 🟡 | `platform_breakout`（`PLATFORM_DAYS`）与 `box_breakout`（`BOX_DAYS`）已有；基准是否严格只用突破前区间需逐个核对 |
| 突破时量能改善 | 🟡 | `volume_breakout` 有量能条件；没有统一「突破量比」字段 |
| 中短期均线转强 | 🟡 | `ma_bull`、`ma_golden_cross` 有布尔；缺 MA20 斜率拐点字段 |
| RPS 由弱转强 | 🟢 | 需 RPS20 逐日序列（回补后） |
| 所属板块同步增强 | 🟢 | 依赖 P0-1 板块状态；板块 RPS20 历史已有 255 天 |
| 低位观察/启动候选/突破确认/回踩观察/信号失效 | 🟢 | 需新建确定性状态机；现有形态不输出阶段 |
| 无未来函数 | 🟡 | 形态引擎本身只用 `<=T`；但 `/patterns/hits` 的 `phase` 筛选**只支持最新交易日**（`api/patterns.py:890`），历史回放会 400 |
| 除权/复权/不连续行情 | ✅ | `build_bars` + `trim_contiguous_tail` 已处理，复用即可 |
| 不用当前成分冒充历史 | 🟡 | `sector_member` 可回补「当时成分」；未回补的日期必须标 `数据不足` |
| 阈值可配置、可历史检验 | 🟡 | 现有形态阈值写死；新状态机必须参数化 |

### P0-4 板块与个股共振

| 需求 | 状态 | 依据 / 缺口 |
| --- | --- | --- |
| 先选板块，再筛趋势/低位股 | 🟡 | `sector_stock` 自动取 RPS20 前 N 强板块，不能指定任意板块；`/sectors/members` 能看成分但不能筛趋势 |
| 分别保留板块强度/个股强度/共同改善 | 🟡 | 现只有 `sector rps20` + 个股当日涨幅/因子分；没有个股 RPS、没有 5 日改善量 |
| 多板块归属去重并展示各板块 | ⚠️ | `pools.sector_stock` 的 `seen` 先到先得（`pools.py:1472`），一只票只保留首个板块，隐藏其余归属 |
| 结果进入详情/自选/计划 | 🟡 | `StockLink` 可跳详情；加自选/加计划只在部分页面有入口 |
| 历史验证 | 🟡 | `sector_stock_track` 只有 1 日；需要 5/10/20/40/60 日 |
| 不重复建设 | ✅ | 复用 `sector-rps`、`sector-stock`、`sector_member`，不新建第三套数据管线 |
| 不用「热门板块」直接判定共振 | ⚠️ | 现有实现按「板块 RPS ≥90」选股，文档已标注负超额；新实现必须同时要求个股自身趋势/相对强度改善，并如实展示未满足项 |

### P0-5 中短线策略历史验证

| 需求 | 状态 | 依据 / 缺口 |
| --- | --- | --- |
| 信号触发数量 | 🟡 | 各模块各自统计；无统一口径 |
| 5/10/20/40/60 日收益 | 🟡 | `pattern_track` 1~30；`scorecard` 1/3/5/10；`sector_rps` 5/20；`first_board` 次日；**没有 40/60** |
| 平均/中位/正收益占比/分布 | 🟡 | 首板有均值/中位/正收益；形态成绩单无中位、无分布 |
| 相对基准超额 | ✅ | 同日全市场等权基准已有（`pattern_track._matrix`、`sector_rps.track`） |
| 最大不利波动 MAE | 🟢 | 矩阵里有逐日 `pct_chg`，可算持有期内最低累计收益 |
| 不同市场环境 | 🟢 | `market_sentiment` + `index_daily` 可做确定性环境分类；未接入回测 |
| 样本外 / walk-forward | 🟡 | 脚本有 `--since`；无 API、无参数保存、无滚动验证 |
| 信号日/次日执行/观察收益/扣费收益 | 🟡 | `backtest_patterns` 支持 `next_open` 与 `skip-unbuyable`；`pattern_track` 只有收盘口径；UI 无费用/滑点 |
| 未来函数 | ⚠️ | 形态/板块回测基本 as-of；但 `/patterns/hits` 的 phase 仅最新日、`stock_phase` 缓存按最新日线版本，历史回放要单独路径 |
| 幸存者偏差 | ⚠️ | 回测脚本用当前 `stock_universe`/当前代码池；`history.db` **0 只退市股**（最后一根都 ≥2026-06-30）；ST 用最后一根名字整段剔除 |
| 样本选择偏差 | 🟡 | 20 日去重、`pattern_track_start`/`pool_track_start` 有约束；不同模块起点不一致 |
| 小样本不稳定 | 🟡 | `scorecard` 有样本阈值；无置信区间/标准误 |
| 重复股票 | 🟡 | `scorecard` 去重；`pattern_track` 故意不去重（回答「每天买会怎样」）；需在 UI 明确 |
| 不可成交价格虚高 | 🟡 | 首板/接力回测有涨停买不进剔除；形态跟踪没有 |
| 参数过拟合 | 🔴 | 没有样本外参数选择流程；现有阈值多为人工拍定 |

### 旧任务书条目的重新定位

| 旧条目 | 新定位 |
| --- | --- |
| 多条件题材交叉挖掘（AND/OR） | **P1**（数据支持，但历史概念归属只对涨停股可靠） |
| 热点主线持续性（涨停题材） | 被 P0-1 板块状态覆盖一部分；涨停题材保留为辅助 |
| 强势股次日溢价 / 连板晋级 | **P2 保留**，不优先重构 |
| 资金量价共振 | 拆入 P0-1（板块资金一致性）与 P0-2（个股量价） |
| 赚钱/亏钱效应 | **P2 辅助判断**，不新建情绪指数 |
| 历史题材复盘 | P1 |
| 策略验证增强 | 升级为 **P0-5** |
| 复盘工作台整合 | P1 |
| 导出 / 跨页状态 / 新闻检索 | P2 |

---

## 4. 历史数据覆盖与数据源限制

### 4.1 本地库实测（只读 `mode=ro` 查询）

| 表 | 行数 | 起始 | 截止 | 备注 |
| --- | ---: | --- | --- | --- |
| `stock_daily` | 2,547,042 | 2024-09-02 | 2026-10-09 | 506 个交易日、5,303 只；≥250 根的有 5,033 只；逐日 `name` 非空 100% |
| `history.db.stock_daily` | 5,820,960 | 2021-09-14 | 2026-10-19 | 1,230 个交易日、5,108 只；≥250 根 4,991 只；**0 只最后日期 <2026-06-30（无退市股）** |
| `sector_daily` | 94,771 | 2025-09-08 | 2026-09-24 | 255 天；精选 279 + 行业 104；`strength/amount/pct_chg` 100% 有值；`net_inflow/member_count` 仅 724 行；`up/down_count` 全空 |
| `sector_member` | 14,707 | 2026-09-18 | 2026-09-24 | 仅 5 天、33 个板块、4,673 只票 |
| `sector_fund_flow` | 953 | 2026-09-22 | 2026-09-23 | 仅 2 天 |
| `limit_pool` | 28,225 | 2025-09-08 | 2026-09-24 | 255 天 |
| `rps_daily` | 10,206 | 2026-10-09 | 2026-10-21 | **仅 2 个扫描日** |
| `pattern_hit` | 20,637 | 2026-05-14 | 2026-10-09 | 26 个有命中日，不连续 |
| `index_daily` | 1,530 | 2025-09-08 | 2026-09-24 | 6 个指数：上证/深成/创业板/科创50/中证1000/北证50 |
| `stock_concept` | 25,382 | 2025-03-05 | 2026-09-28 | 只覆盖涨停股 |
| `market_sentiment` | 255 | 2025-09-08 | 2026-09-24 | 与板块日线同窗口 |
| `trade_calendar` | 8,797 | 1990-12-19 | 2026-12-31 | **含未来日期**，任何「按日历取最近 N 天」都必须用实际数据日封顶 |

> 说明：本地库是开发快照，生产库可能更新。阶段 B 开始前必须在目标环境用只读查询复核一次；**本报告不假设生产库与本地一致**。

### 4.2 数据源限制

| 数据 | 来源 | 限制 |
| --- | --- | --- |
| 个股日线 | iFinD（主库）+ 腾讯（`history.db`） | 主库首次/长历史 500 日、保留 520 日、每日回补 4 个交易日；history.db 只有当前代码池，**无退市股** |
| 板块日线 | 开盘红（免费） | 2025-09-08 起；`up_count/down_count` 不提供 |
| 板块成分 | 开盘红 `ZhiShuStockList_W8`（免费，支持历史） | 回补 0.38 秒/板块、一天约 2.4 分钟；120 天约 3.5 小时；不占 iFinD 配额 |
| 板块资金流 | 成分股 × 个股 DDE | 只有实时/当日链路，**没有历史回补接口**；只能从上线日起积累 |
| RPS | 本地计算 | 零外部请求；历史需逐日回补（CPU/IO，不耗配额） |
| 涨停/首板 | akshare/东财 | 仅约 15 个交易日明细保留；本轮不扩采集 |
| 新闻/公告 | 东财按需 | 无历史索引、无批量授权；页面打开时逐只请求不可接受 |

### 4.3 对 P0 的直接影响

1. **P0-1 广度**：本地不可用；生产若已按文档回补 60 天，可用最近 60 天。未回补日期必须显示 `数据不足`，不能填 0。
2. **P0-1 资金一致性**：只能做「自 2026-09-22 起」的短样本，且必须标注样本起点；不能与价格历史等长比较。
3. **P0-4 共振历史**：依赖 `sector_member` 逐日快照；60 天样本只能回答「近三个月」，不能外推。
4. **P0-5 长历史**：`history.db` 可做 5 年价格回测，但没有退市股、没有历史板块成分、没有历史 ST 状态（逐日 name 可补 ST）。结论只能用于「同口径横向比较」，不能当作实盘收益。
5. **5~60 日持有期**：`stock_daily` 主库足够；板块 60 日窗口也足够（255 天）；120 日以上板块窗口不足，本轮 P0-1 只要求到 60 日。

---

## 5. 总体设计

### 5.1 设计原则

1. **一个口径只写一份**：RPS、前复权 K 线、基准矩阵、ST 判定、板块强度全部复用现有函数；新模块只做「特征组合与状态判定」。
2. **as-of 严格**：任何特征只用 `<= T` 的行；板块归属、RPS、ST 名称、指数基准都取 T 日或之前最近可用值，并记录 `as_of`。
3. **缺失即缺失**：返回 `None` + `missing[]` + `数据不足` 状态，禁止 0 填充。
4. **可解释**：每行返回 `reasons[]`（命中依据，带实际值）、`unmet[]`（未满足的关键条件）、`missing[]`（数据缺口）、`invalidations[]`（判断失效条件）。
5. **先验证后展示**：P0-1~P0-4 的每个候选池，都必须同时提供同一套参数的 `track` 接口；页面默认显示样本数、区间、基准与已知偏差。
6. **不新增外部请求**：所有新接口只读本地库。板块成分回补是一次性离线脚本，需单独批准。
7. **不混口径**：精选板块/行业、板块指数收益/个股收益、观察性收益/可交易收益，全部在响应里显式标注。

### 5.2 新增/修改模块总览

| 类型 | 路径 | 作用 |
| --- | --- | --- |
| 新增 service | `backend/app/services/trend_features.py` | 趋势特征纯函数：均线、斜率、区间收益、相对强度、量能、风险状态、解释字段 |
| 新增 service | `backend/app/services/low_start.py` | 低位启动状态机（纯函数，输入 Bars + 上下文） |
| 新增 service | `backend/app/services/sector_state.py` | 板块轮动指标 + 状态分类 + 排名序列 + 核心股变化 |
| 新增 service | `backend/app/services/resonance.py` | 板块→个股共振（复用 `pools.sector_stock` 的成员筛选，输出多板块归属） |
| 新增 service | `backend/app/services/research_backtest.py` | 统一前瞻收益与统计（5/10/20/40/60、分布、MAE、环境、费用） |
| 修改 service | `backend/app/services/sector_rps.py` | 窗口扩到 5/10/20/60；补 `window_values` 通用化；`board` 增加 rank/日期字段 |
| 修改 service | `backend/app/services/pools.py` | `SectorStockRow` 增加多板块列表与个股强度；新增参数化入口（不破坏旧接口） |
| 修改 job | `backend/app/jobs/scan_rps.py` | 增加 20/60 日窗口与列写入 |
| 新增 script | `scripts/backfill_rps.py` | 按日回补 RPS20/60/120 历史（零外部请求） |
| 新增 script | `scripts/backtest_research.py` | 命令行调用统一回测（趋势/低位/共振），复用生产服务 |
| 新增 API | `backend/app/api/research.py` | 统一研究接口（可选，或分散到 sector/pools） |
| 修改 API | `backend/app/api/sector.py` + `pools.py` | 增加状态、趋势池、低位池、共振池、track 接口 |
| 修改 models | `backend/app/models.py` | `RpsDaily` 加 `ret20/ret60/rps20/rps60` 可空列 |
| 修改前端 | `frontend/src/pages/Sectors.tsx`、`Pools.tsx`、`StockDetail.tsx`、`Dashboard.tsx` | 状态/候选池/解释/跨页状态 |
| 新增组件 | `frontend/src/components/SectorStatePanel.tsx`、`TrendPoolPanel.tsx`、`LowStartPanel.tsx`、`ResonancePanel.tsx`、`BacktestSummary.tsx` | 复用 `Panel`/`SortTh`/`EChart`/`StockLink` |
| 修改 types/client | `frontend/src/api/types.ts`、`client.ts` | 新响应类型与请求函数 |

### 5.3 配置项（新增 `config.py`）

所有阈值集中、可被 API 覆盖、可在回测中复用；默认值只是起手值，不写成业务常量散落各处。

```python
# 板块状态
sector_state_strong_rps = 90.0          # 新晋/持续强势的分位线
sector_state_watch_rps = 60.0           # 强势整理/回落观察线
sector_state_weak_rps = 20.0
sector_state_strong_days = 3            # 连续强势天数
sector_state_rank_change = 10           # 排名变化阈值
sector_state_amount_ratio = 1.0         # 成交额/20 日均值

# 趋势池
trend_min_bars = 130                    # MA120 + 余量
trend_ma_fast, trend_ma_mid, trend_ma_slow = 20, 60, 120
trend_slope_days = 5
trend_min_trend_days = 5
trend_min_rps20 = 60.0
trend_min_rps60 = 60.0
trend_max_off_high = 25.0               # 距 60 日高点回撤上限（%）
trend_min_amount_ratio = 0.8
trend_min_avg_amount = 2e7              # 与 stock_universe 同一门槛
trend_max_float_mv = None

# 低位启动
low_range_window = 250
low_position_max = 35.0                 # 区间位置 ≤35% 视为低位
low_platform_min_days = 15
low_platform_max_height = 18.0          # 平台振幅（%）
low_vol_contraction = 0.8               # ATR20/ATR60
low_breakout_buffer = 1.0               # 收盘 > 平台高 × buffer
low_breakout_volume = 1.3               # 突破日成交额/20 日均值
low_pullback_days = 10
low_invalidate_buffer = 0.95            # 跌破平台高 ×0.95 失效

# 统一回测
backtest_horizons = (5, 10, 20, 40, 60)
backtest_entry = "next_open"            # close 仅作理论对照
backtest_cost_bps = 0                   # 默认 0，页面可填；>0 时扣双边
backtest_dedup_days = 20
backtest_min_samples = 30
```

### 5.4 数据库变更评估

| 变更 | 必要性 | 风险 | 结论 |
| --- | --- | --- | --- |
| `rps_daily` 加 `ret20/ret60/rps20/rps60` | P0-2/P0-3 必需；避免每次请求全市场重算 | 可空列，`db._add_missing_columns` 支持；不破坏旧行 | **建议做** |
| 新建 `sector_metric_daily` | 若状态接口超过 500ms 再考虑 | 新表、需调度与回补 | **先不做**，用进程内缓存；实测超时再评估 |
| 新建 `research_run` | P1 保存策略条件与历史对比 | 需要用户维度与权限 | **P1 再做** |
| 新建索引 | `sector_member(trade_date, sector_code)` 已被主键覆盖；`stock_daily(code, trade_date)` 已有 | 低 | 暂不新增；上线前用 `EXPLAIN QUERY PLAN` 验证 |
| 修改既有主键/列型 | 不需要 | 高 | **禁止** |

> 生产保护：任何回补脚本只写本地/目标库的独立副本；阶段 B 不触碰生产。`RpsDaily` 加列由启动时自动迁移完成，但回补脚本必须可重复执行（upsert）。

---

## 6. P0-1 板块轮动研究系统 · 详细设计

### 6.1 指标与计算规则

对每个 `taxonomy`（`kph_selected` / `kph_industry`）独立计算，禁止跨口径排名。

记 `d_t` 为交易日，`s` 为板块：

1. **区间收益** `ret_N(s,t)`：
   `∏(1 + pct_chg/100) − 1`，窗口为 `d_{t-N+1} … d_t`；窗口内有效行数 < 90% → `None`。
   N ∈ {5, 10, 20, 60}。复用 `services/rps.ret/window_values`。
2. **相对强度** `rps_N(s,t)`：`ret_N` 在当日同 `taxonomy` 全部有值板块中的中位秩百分位（0~100）。
3. **排名** `rank_N(s,t)`：按 `ret_N` 降序，1 起；并列按代码稳定排序。`rank_change_5 = rank_N(t-5) − rank_N(t)`（正=上升）。**排名变化只作描述，不单独构成买卖信号。**
4. **成交额比值** `amount_ratio_20(s,t) = amount_t / mean(amount_{t-20…t-1})`；分母 ≤0 或缺失 → `None`。
5. **内部广度** `breadth_up_ratio(s,t) = count(member.pct_chg > 0) / count(member.pct_chg 非空)`：
   - 只读 `sector_member` 当天快照；
   - 当天该板块无快照 → `None` + `missing=["sector_member@date"]`；
   - 不得用 `sector_daily.up_count` 冒充（该列全空）。
6. **资金一致性**：
   - `net_inflow_t` 仅当 `net_inflow` 所在日期 == `t` 时使用；
   - `inflow_consistency = sign(net_inflow_t) == sign(pct_chg_t)`；
   - `inflow_5d = sum(net_inflow)` 仅当连续 5 天都有值；
   - 任何缺日 → `None` + `missing`；**不把不同来源的资金流相加**（本项目只有开盘红/成分×DDE 一套，仍要标注来源日）。
7. **连续强势天数** `strong_days`：从 `t` 往前数，连续满足 `rps20 ≥ watch_rps` 的交易日数；遇到缺行或低于阈值即停。
8. **核心股**：从 `sector_member` 当天取该板块成员，按 `pct_chg` 降序前 5、按 `amount` 降序前 5，并附个股 `rps20`（若 `rps_daily` 有当天值，否则 `None`）；`core_change` = 与 5 个交易日前的前 5 名单交集比例（无历史成员 → `None`）。

### 6.2 状态分类（优先级从高到低）

| 状态 | 规则（默认阈值，全部可配） | 说明 |
| --- | --- | --- |
| `数据不足` | `ret20` 或 `rps20` 为 `None`；或核心指标窗口覆盖 <90% | 独立状态，不参与强弱排序 |
| `新晋强势` | `rps20 ≥ 90`，且过去 5 个交易日的 `max(rps20) < 80`；或 `rank20` 从 >30 进入前 10 | 强调「刚进入强势区」 |
| `持续走强` | `rps20 ≥ 80` 且 `strong_days ≥ 3`，且 `rank_change_5 ≥ 0` | 不要求排名每天上升 |
| `强势整理` | `rps20 ≥ 60` 且 `|ret5| ≤ 3%` 且 `amount_ratio_20 < 1.0` | 强势中的缩量横盘 |
| `强度回落` | `rps20 ≥ 60` 但 `rps20` 较过去 5 日最高值下降 ≥ 15 分位，或 `rank_change_5 ≤ -10` 且 `ret5 < 0` | **只描述强度回落，不判定趋势结束** |
| `持续弱势` | `rps20 ≤ 20` 且 `strong_days = 0` 且 `ret20 < 0` 连续 ≥3 天 | |
| `中性` | 其余 | |

`reasons[]` 例：`["rps20=93.2 ≥ 90", "5日前最高 rps20=61.4 < 80", "rank20=4 (5日前 37)"]`。
`missing[]` 例：`["sector_member@2026-09-24"]`。
`invalidations[]` 例：`["rps20 < 60 连续 2 日", "收盘跌破 20 日均线"]`。

### 6.3 接口

```text
GET /api/sector/rotation-state
  taxonomy=kph_selected|kph_industry
  date=YYYY-MM-DD（缺省=该口径最新板块日）
  windows=5,10,20,60（固定集合，避免任意窗口导致口径漂移）
  lookback=20（排名变化与 strong_days 的回看交易日）
  state=新晋强势,持续走强,...（可选，多选并集）
  order=rps20|ret20|amount_ratio_20|rank_change_5
  limit=50&offset=0

GET /api/sector/rank-series
  code=板块代码&taxonomy=&days=60
  → dates[], rank20[], rps20[], ret5/20[], amount_ratio_20[]
```

响应统一带 `taxonomy`、`as_of`、`sample_days`、`data_start`、`warnings[]`。

### 6.4 前端

在 `Sectors.tsx` 的 `view='boards'` 下新增「板块状态」面板（不新建页面）：
- 顶部：口径切换、交易日、窗口多选、状态筛选；
- 表格列：板块、状态、RPS5/10/20/60、排名及 5 日变化、成交额比、广度（含日期）、资金一致性（含日期）、核心股；
- 点板块 → 复用现有右侧详情与「加入对比」；点核心股 → `StockLink`，URL 带 `date/taxonomy/state`；
- 状态标签 hover 显示 `reasons/unmet/missing`；
- 排序、分页、空态、错误态、窄屏折叠。

### 6.5 性能

- 单次请求：374 板块 × 60 交易日的 `pct_chg` 约 2.2 万行，Python 计算 <100ms；`sector_member` 当天最多 ~6 万行，按板块聚合 <100ms。
- 使用进程内缓存：键 = `(taxonomy, date, max(sector_daily.trade_date), max(sector_member.trade_date))`，数据版本变化即失效（与 `stock_phase._PHASE_CACHE` 同一思路）。
- 不新增外部请求，不写入数据库。

---

## 7. P0-2 趋势股筛选系统 · 详细设计

### 7.1 特征层（纯函数，输入 `Bars` 截至 T）

`services/trend_features.py`：

| 特征 | 公式 |
| --- | --- |
| `ma20/60/120` | 收盘简单均值；不足窗口 → `None` |
| `ma20_slope` | `(ma20_t / ma20_{t-5} − 1) × 100`；`ma60/120` 同理 |
| `close_vs_ma20/60/120` | `(close/ma − 1) × 100` |
| `ma_order` | `close > ma20 > ma60 > ma120` 等布尔组合 |
| `trend_days` | 从 t 往前连续满足 `close > ma20 且 ma20_slope > 0` 的交易日数 |
| `ret20/60/120` | `pct_chg` 复利；覆盖率 <90% → `None` |
| `rps20/60/120` | 当日全市场横截面中位秩（优先读 `rps_daily`；历史回测用回补后的表） |
| `excess_sector_20` | `ret20(个股) − ret20(所属最强板块指数)`；板块归属缺失 → `None` |
| `excess_index_20` | `ret20(个股) − ret20(基准指数)`；基准可选上证/创业板/中证1000 |
| `amount_ratio_20` | `amount_t / mean(amount_{t-20…t-1})` |
| `amount_ratio_breakout` | 突破日 `amount / 前 20 日均值`（仅当日前命中突破形态时） |
| `turnover_20` | 20 日 `turnover` 均值 |
| `off_high_60` | `(close / max(high_{t-59…t}) − 1) × 100` |
| `risk_flags[]` | `st`、`bse`、`low_liquidity`、`short_history`、`bad_price`、`trend_deterioration` |

### 7.2 风险过滤规则

| 风险 | 判据 | 处理 |
| --- | --- | --- |
| ST | 当日 `stock_daily.name` 含 `ST`（**不用最新名字**） | 默认排除；回测同样按当日 |
| 北交所 | `limit_rules.is_bse(code)` | 默认排除 |
| 历史不足 | 可用 K 线 < `trend_min_bars` | 标记 `short_history`，不参与排名 |
| 流动性过低 | 20 日平均成交额 < `trend_min_avg_amount` | 默认排除 |
| 价格异常 | `close<=0`、`pct_chg<=-100`、OHLC 缺失 | 标记 `bad_price`，不计算 |
| 趋势恶化 | `close < ma60` 且 `ma60_slope < 0`；或 `off_high_60 ≤ -30%` | 默认不进入「趋势池」，但可在「风险观察」中显示 |
| 退市风险 | 无可靠历史退市名单 | 如实标 `delisting_unknown`（见 P0-5 限制） |

### 7.3 筛选与排序

- 所有条件来自 query/配置，不写死；默认：
  - `ma_order=close>ma20>ma60`（MA120 可选）；
  - `rps20 ≥ 60`、`rps60 ≥ 60`；
  - `trend_days ≥ 5`；
  - `amount_ratio_20 ≥ 0.8`；
  - `off_high_60 ≥ -25%`；
  - 排除 ST/北交所/低流动性/短历史。
- 排序默认 `rps60 desc, trend_days desc, code`；**不提供黑盒综合分**。若要排序分，单独字段 `rank_score` 并展示公式与「仅排序，不预测收益」。
- 分页：`limit ≤ 200`、`offset`；总数单独返回。

### 7.4 接口

```text
GET /api/pools/trend
  date=, min_rps20=, min_rps60=, min_trend_days=, ma_order=,
  min_amount_ratio=, max_off_high=, min_avg_amount=, max_float_mv=,
  exclude_st=true, exclude_bse=true, only_above_ma60=true,
  order=, limit=, offset=

GET /api/pools/trend/track
  同筛选参数 + horizons=5,10,20,40,60 + entry=next_open|close
  + cost_bps= + dedup_days= + benchmark=market|index:000001.SH
```

响应行必须包含 `reasons[] / unmet[] / missing[] / invalidations[]`，例：
`reasons=["close 12.4 > ma20 11.8 > ma60 10.9", "rps60=78.5", "trend_days=11", "amount_ratio_20=1.42"]`
`unmet=["MA120 仍向下 (-1.2%)", "距 60 日高点 -18%"]`
`invalidations=["收盘跌破 ma20 连续 2 日", "rps60 < 50"]`

### 7.5 RPS20/60 的实现选择

**方案 A（推荐）**：扩展 `RpsDaily` 与 `scan_rps.WINDOWS`，新增 `ret20/ret60/rps20/rps60`；写 `scripts/backfill_rps.py` 按日回补最近 250 个交易日。
- 优点：接口快、历史可验证、与现有 RPS 完全同口径；
- 成本：一次性 CPU/IO；每日扫描多算两个窗口（约 +30% 时间，仍为秒级）；
- 风险：自动加列只加可空列，安全。

**方案 B（备选）**：请求时现算。5000 只 × 120 根 ≈ 60 万行，单次 2~5 秒，不适合列表页与回测。

结论：采用 A，B 仅作为回补脚本内部实现。

### 7.6 前端

在 `Pools.tsx` 新增「趋势股」面板（与现有四块并列，不新建页面）：
- 筛选条：RPS20/60、趋势天数、均线排列、成交额比、距高点、市值、排除 ST/北交所；
- 表格：代码/名称/行业/收盘/涨跌/MA20/60/120/斜率/RPS20/60/120/趋势天数/量比/距高点/风险标记；
- 行展开：`reasons/unmet/missing`；
- 操作：加入自选、加入交易计划（复用 `POST /plans/from-signal` 或新增 `from-pool`）、复制代码、导出 CSV（P2 再做）；
- URL 保存 `date/params/order`；
- 底部「历史验证」折叠面板，复用统一回测摘要组件。

---

## 8. P0-3 低位启动识别系统 · 详细设计

### 8.1 特征（全部截至 T）

| 特征 | 公式 / 说明 |
| --- | --- |
| `pos_pct` | `(close − min(low_{T-N+1…T})) / (max(high_{T-N+1…T}) − min(low_{T-N+1…T})) × 100`，N=`low_range_window`(250) |
| `platform` | 在 `[T-P, T]` 内，若 `max(high)/min(low) − 1 ≤ low_platform_max_height` 且 P ≥ `low_platform_min_days`，视为平台；输出 `platform_days`、`platform_high`、`platform_low`、`platform_height` |
| `vol_contraction` | `ATR20 / ATR60`（ATR=平均真实波幅）；或 `std(ret20)/std(ret60)`；≤0.8 视为收敛 |
| `breakout_ref` | **只用 `[T-P, T-1]`** 的平台高（不含当日），避免用突破日自身抬高基准 |
| `breakout` | `close_T > breakout_ref × buffer`（默认 buffer=1.0）；或 `high_T > ref` 且 `close_T ≥ ref×0.99` |
| `volume_ratio` | `amount_T / mean(amount_{T-20…T-1})` |
| `ma_turn` | `ma20_slope > 0` 且 `close > ma20`；可选 `ma20 > ma60` |
| `rps20_delta5` | `rps20_T − rps20_{T-5}`；需 RPS 历史 |
| `sector_state` | 所属最强板块的 P0-1 状态与 `rps20_delta5`；板块归属缺失 → `None` |
| `invalidation_level` | `platform_low` 或 `breakout_ref × low_invalidate_buffer` |

### 8.2 状态机（优先级）

| 状态 | 规则 |
| --- | --- |
| `数据不足` | 可用 K 线 < `low_range_window` 或平台无法定义；仍输出已算出的字段 |
| `信号失效` | `close < invalidation_level`，或 `close < ma60` 且 `ma60_slope < 0` |
| `突破确认` | `breakout` 且 `volume_ratio ≥ low_breakout_volume` 且 `ma_turn`；可选要求 `rps20_delta5 > 0` / 板块状态为强势 |
| `回踩观察` | 过去 `low_pullback_days` 内出现过「突破确认」，当前 `close ∈ [ref×0.95, ref×1.05]`，`volume_ratio < 0.8`，且 `close > ma20` |
| `启动候选` | `pos_pct ≤ 35`、平台成立、`vol_contraction ≤ 0.8`，且满足以下任一：`close ≥ platform_high×0.98`、`volume_ratio ≥ 1.2`、`ma20_slope` 由负转正 |
| `低位观察` | `pos_pct ≤ 35` 且平台成立，但未触发上述条件 |
| `中性` | 其余 |

**说明**：状态不是严格时序，允许「低位观察 → 启动候选 → 突破确认 → 回踩观察」，也允许直接失效；文档与 UI 必须写明。

### 8.3 历史与未来函数约束

- 所有窗口 `<= T`；`breakout_ref` 不含 T；
- 不用未来 K 线确认历史信号；
- 复权：复用 `build_bars`（已前复权 OHLC，量同步缩放）；
- 不连续行情：复用 `trim_contiguous_tail`；不足则 `数据不足`；
- 板块归属：用 T 日 `sector_member`；缺失不补；
- 阈值全部来自配置/query，回测可覆盖。

### 8.4 接口

```text
GET /api/pools/low-start
  date=, state=低位观察,启动候选,突破确认,回踩观察,信号失效,
  min_pos=, max_pos=, min_platform_days=, max_platform_height=,
  max_vol_contraction=, min_volume_ratio=, require_sector_strong=false,
  order=pos_pct|volume_ratio|rps20_delta5, limit=, offset=

GET /api/pools/low-start/track
  同参数 + horizons + entry + cost_bps + dedup_days
```

### 8.5 前端

`Pools.tsx` 新增「低位启动」面板：状态分组（tabs/折叠），每行显示 `pos_pct`、平台天数/振幅、波动收敛、突破基准、量比、RPS 改善、板块状态；行内展开解释与失效条件；点击进入个股详情并保留 `date/state`。

---

## 9. P0-4 板块与个股共振 · 详细设计

### 9.1 流程

1. **选板块**：从 P0-1 状态接口选 `新晋强势/持续走强`，或手动勾选板块（`sector[]` 参数）；
2. **取成分**：用 T 日 `sector_member`（不是当前快照）；缺失日期直接返回 `数据不足`；
3. **筛个股**：对成分股调用 P0-2 趋势特征 / P0-3 低位特征（同一套纯函数）；
4. **排序**：默认 `sector_rps20 desc, stock_rps60 desc, trend_days desc`；不输出黑盒分。

### 9.2 输出字段

- 板块：`sector_code/name/rps20/rps60/state/rank/rank_change_5/amount_ratio_20/breadth`
- 个股：`code/name/sectors[]`（**全部命中板块，不再只留首个**）、`sector_rps20_max`、`stock_rps20/60`、`excess_sector_20`、`trend_state`、`low_start_state`
- 共同改善：`co_improve` = `{sector_rps20_delta5, stock_rps20_delta5, both_positive: bool}`；**分别展示，不合成单一分数**
- 解释：`reasons/unmet/missing/invalidations`

### 9.3 与现有实现的关系

- 保留 `pools.sector_stock` 作为「前 N 强板块 × 涨幅/因子前 N」的研究视图，不删除；
- 新增 `resonance` 服务，内部复用 `sector_stock` 的成员读取与 ST/BSE 剔除逻辑，但：
  - 支持用户指定板块列表；
  - `SectorStockRow` 增加 `sectors[]`，去重但保留全部归属；
  - 增加个股趋势/低位状态；
  - 跟踪期扩展到 5/10/20/40/60。
- 不新建独立数据管线，不复制板块/成员表。

### 9.4 接口

```text
GET /api/pools/resonance
  date=, taxonomy=, sectors=code1,code2（可空=按状态取）
  sector_states=新晋强势,持续走强
  stock_view=trend|low_start|all
  min_sector_rps20=, min_stock_rps60=, min_trend_days=
  order=, limit=, offset=

GET /api/pools/resonance/track
  同参数 + horizons + entry + cost_bps
```

**验收要求**：页面必须显示「板块强 ≠ 个股强」的历史证据与当前样本数；不得写成买入推荐。

---

## 10. P0-5 中短线策略历史验证 · 详细设计

### 10.1 统一口径（所有策略共用）

| 项 | 规则 |
| --- | --- |
| 信号日 T | 只用 `<= T` 的数据；T 收盘后生成信号 |
| 入场 | 默认 `next_open`：T+1 开盘价；T+1 停牌 → 顺延到复牌首日开盘；复牌首日开盘涨停 → 记为 `unbuyable`，不计入收益 |
| 卖出 | T+h 的收盘价；停牌 → 顺延到复牌后首个交易日收盘；数据末端 → `无法退出`，该档不计入均值 |
| 持有期 | h ∈ {5,10,20,40,60} 个交易日，按 `TradeCalendar` 定位，不按行数 |
| 收益 | 个股用 `pct_chg` 复利（前复权口径）；组合等权平均 |
| 基准 | 同一入场日的「全市场等权」；可选指数基准；基准同口径剔除不可成交样本 |
| 费用 | `cost_bps` 双边扣除；默认 0，页面可填；滑点可用固定 bps 或按成交额分档（P1） |
| 去重 | 同一 (策略, 代码) 在 `dedup_days` 内只算首个信号；另给「不去重」口径用于回答「每天买会怎样」 |
| 样本状态 | `ok / pending / unbuyable / no_quote / suspended / no_exit`，分别计数，不混入 0 收益 |
| 分布 | ≤−10、−10~−5、−5~0、0~5、5~10、≥10（%）的样本数与占比 |
| MAE | 持有期内每日累计收益的最小值（等权组合或个人样本的中位） |
| 市场环境 | 用信号日 `index_daily` 与 `market_sentiment` 的确定性规则分 `进攻/震荡/防守`；规则写入配置并在响应中展示 |
| 样本外 | `oos_start` 参数；报告同时给全样本、样本内、样本外；P1 再做 walk-forward |

### 10.2 结构

`services/research_backtest.py`：

```python
@dataclass
class StrategyResult:
    strategy: str
    params: dict
    sample_days: int
    samples: int
    horizons: dict[int, HorizonStat]
    regimes: dict[str, dict[int, HorizonStat]]
    skipped: dict[str, int]
    warnings: list[str]

def run(session, strategy, params, start, end, horizons, entry, cost_bps,
        dedup_days, benchmark) -> StrategyResult
```

内部：
1. 生成交易日序列（`TradeCalendar` ∩ 数据日）；
2. 对每个 T 调用对应生产服务（`trend_pool.signals` / `low_start.signals` / `resonance.signals`）；
3. 一次读取 T+1…T+max(h) 的行情矩阵（复用 `pattern_track._matrix` 的思路，必要时扩展为支持任意 horizons）；
4. 计算每样本每 horizon 的收益、MAE、状态；
5. 聚合统计与分布；
6. 按信号日环境分组。

### 10.3 接口

```text
GET /api/research/backtest
  strategy=trend|low_start|resonance|pattern|first_board|sector_rps
  start=, end=, oos_start=（可选）
  horizons=5,10,20,40,60
  entry=next_open|close
  cost_bps=0
  dedup_days=20
  benchmark=market|index:000001.SH
  params=JSON（策略参数，后端按策略白名单校验）
```

响应：`StrategyResult` + `data_coverage` + `warnings`。
缓存键：`(strategy, params, start, end, entry, cost_bps, 数据版本)`；数据版本 = 最新 `stock_daily` 日 + 行数 + 最新 `rps_daily` 日（与 `stock_phase._PHASE_CACHE` 同思路）。

### 10.4 修正现有偏差

| 偏差 | 修法 | 阶段 |
| --- | --- | --- |
| 幸存者偏差（用当前 `stock_universe`） | 回测股票池改为「T 日有行情行 + 前 20 日满足流动性」的**当日可用池**；不使用当前 `stock_universe` | B0 |
| ST 用最新名字整段剔除 | 用 `stock_daily.name` 当日值判 ST | B0 |
| 退市股缺失 | 现有库无解；在报告与 UI 明示；P2 评估补退市清单 | P2 |
| 未来函数 | 所有特征/状态只读 `<=T`；`/patterns/hits` 历史 phase 另走 as-of 路径或禁用 | B0/B2 |
| 不可成交 | 统一 `next_open` + 开盘涨停剔除；`close` 只作理论对照 | B0 |
| 费用/滑点 | `cost_bps` 双边扣除；报告给 0 与含费两套 | B0 |
| 重复样本 | 默认 `dedup_days=20`，另给不去重口径 | B0 |
| 小样本 | `min_samples=30`；报告样本数与置信区间（P1） | B0/P1 |
| 参数过拟合 | 强制 `oos_start`；P1 做 walk-forward；禁止按全历史最优选参数 | B1+ |

### 10.5 验收标准

- 同一策略、同一参数，`/api/research/backtest` 与命令行脚本结果一致（容差 0.01pp）；
- 修改 T 之后的数据，不改变 T 日信号（自动化测试）；
- `next_open` 与 `close` 两套收益同时展示，且明确标注哪套可交易；
- 所有统计都带 `samples / pending / unbuyable / no_quote`，不把缺失当 0。

---

## 11. P1 / P2 处置

### P1（P0 验收后）

1. **多题材交叉挖掘**：新增 `GET /api/stocks/theme-search`，支持 AND/OR、范围（名称/概念/行业/涨停原因）、多条件筛选、分页；数据用 T 日 `sector_member` + `stock_concept` + `limit_reason`。历史日期缺快照 → 只返回当时可用来源并标注；**禁止用当前归属冒充历史**。
2. **个股趋势阶段**：在个股页展示 P0-2 特征与 P0-3 状态；不新建页面。
3. **趋势与量价共振解释**：将 `reasons/unmet` 做成统一组件。
4. **历史板块轮动回放**：在 `Sectors` 选择历史日后，严格用该日数据；事后结果单独分区。
5. **工作台整合**：`Dashboard` 四区（市场状态/热点主线/强势股验证/明日计划），复用现有组件，折叠/标签页降密度。
6. **研究结论与计划联动**：`AnalysisSnapshot` 已存在，可扩展为「保存策略条件 + 结果摘要」。
7. **策略条件保存与历史对比**：新表 `research_run`（用户维度），P1 评估。

### P2

1. 研究名单导出：复制代码、CSV、同花顺格式（格式验证后）。
2. 跨页状态保持：`Pools`、`LimitReview`、`StockDetail` 补 URL 参数；跳转个股带 `from/date/params`。
3. 新闻/公告题材检索可行性：先出报告；若无法获得历史覆盖或成本过高，不实施。
4. 涨停晋级/首板溢价/竞价快照：保留现状，仅在明确缺陷时修。

---

## 12. 测试计划

### 12.1 后端

| 类别 | 用例 |
| --- | --- |
| 纯函数 | MA20/60/120、斜率、`trend_days`、`pos_pct`、平台高度、ATR 收敛、量比、RPS 窗口/覆盖率 |
| 状态机 | 板块 6 状态 + 数据不足；低位 5 状态 + 数据不足；优先级与边界值 |
| 无未来函数 | 在 T 之后插入/修改行情，T 日信号不变；`breakout_ref` 不含 T |
| 缺失数据 | 板块成分缺失、资金流缺失、RPS 缺失、停牌、数据末端 → `None/数据不足`，不得为 0 |
| 交易日历 | 跨周末/节假日；预置未来日期不得当数据边界；停牌顺延 |
| 多板块 | 一只票属于多个板块时 `sectors[]` 完整、计数不重复 |
| 回测 | `next_open`、一字板不可买、停牌顺延、无法退出、费用、去重、基准、分布加总、MAE |
| 一致性 | 新趋势池与 `ma_bull`/`trend_template` 在重叠条件上的结论一致；板块 RPS 与现有接口数值一致 |
| 性能 | `/sector/rotation-state`、`/pools/trend` limit=100、`/research/backtest` 250 日 |
| 参数校验 | 非法 taxonomy/窗口/日期/负值 → 400/422；未来日期 → 404 |

### 12.2 前端（vitest + Testing Library）

- 筛选、排序、分页、空态、错误态、加载态；
- URL 参数恢复与刷新保持；
- 表格与图表数据一致；
- 状态标签/解释浮层内容；
- 个股跳转携带来源与日期；
- 窄屏布局（折叠、横向滚动）。

### 12.3 工程命令

```bash
# 后端
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest tests/test_trend_features.py tests/test_low_start.py tests/test_sector_state.py -q

# 前端
cd frontend
npm run test
npm run lint
npm run build
```

阶段 A 已执行：`tests/test_pools_track.py tests/test_sector_stock.py` → **9 passed**。全量回归留到阶段 B 每次提交后执行。

---

## 13. 分阶段实施计划

> 原则：每个阶段先测试后进入下一阶段；不部署、不连生产、不改生产库。

### 阶段 B0：数据与验证地基（建议优先）

1. `RpsDaily` 加 `ret20/ret60/rps20/rps60`；`scan_rps` 扩展窗口；
2. `scripts/backfill_rps.py` 回补最近 250 个交易日（本地/目标库副本）；
3. `research_backtest` 核心：`next_open`、多 horizon、状态计数、分布、MAE、环境、费用、去重、基准；
4. 修正回测股票池与 ST 口径（当日 `stock_daily.name`、当日可用池）；
5. 测试：无未来函数、停牌、不可成交、费用、基准一致性；
6. **交付物**：可复现的基线报告（现有 RPS/形态/首板/板块策略在统一口径下的数字）。

**验收**：同一策略命令行与 API 结果一致；现有测试全绿；基线报告存档。

### 阶段 B1：P0-1 板块轮动

1. `sector_state` 指标与状态机；
2. `/api/sector/rotation-state`、`/api/sector/rank-series`；
3. `Sectors` 新增状态面板与 URL 状态；
4. 测试：6 状态、缺失、口径隔离、性能；
5. `sector/rotation-state/track`（5/10/20/40/60）接入统一回测。

### 阶段 B2：P0-2 趋势股

1. `trend_features` 纯函数；
2. `/api/pools/trend`、`/track`；
3. `Pools` 新增趋势面板；
4. 测试：特征、风险过滤、解释字段、与形态一致性。

### 阶段 B3：P0-3 低位启动

1. `low_start` 状态机；
2. `/api/pools/low-start`、`/track`；
3. `Pools` 新增低位面板；
4. 测试：状态优先级、突破基准无未来、失效条件、复权/停牌。

### 阶段 B4：P0-4 共振

1. `resonance` 服务（指定板块、多板块归属、趋势/低位筛选）；
2. `/api/pools/resonance`、`/track`；
3. `Pools` 新增共振面板；
4. 测试：多板块去重、成员缺失、共同改善字段、与 `sector_stock` 不重复计数。

### 阶段 B5：P0-5 统一验证与页面

1. `/api/research/backtest` 覆盖 trend/low_start/resonance/pattern/first_board/sector_rps；
2. 前端「历史验证」组件（样本、分布、MAE、环境、样本外）；
3. 命令行 `scripts/backtest_research.py`；
4. 全量测试 + lint + build + 性能。

### 阶段 C：P1

多题材交叉、个股趋势阶段、轮动回放、工作台四区、计划联动、条件保存。

### 阶段 D：P2

导出、跨页状态、新闻检索可行性。

---

## 14. 风险与需确认事项

### 14.1 主要风险

1. **有效性风险**：现有证据显示板块动量与 RPS 单独使用不稳定，近 60 日甚至反向。必须把「验证」作为上线门槛，不能把状态/评分包装成买入信号。
2. **数据风险**：`sector_member`、`sector_fund_flow`、`rps_daily` 历史极短；本地与生产可能不一致。任何依赖这些表的页面必须显示数据起点与样本数。
3. **回补成本**：120 天板块成分回补约 3.5 小时、大量外部请求；必须避开交易时段与采集任务，且需单独批准。
4. **幸存者偏差不可完全修复**：无退市股名单；长历史结论只能横向比较，不能当实盘收益。
5. **性能风险**：全市场逐日特征计算量大；必须批量取数、向量化、缓存，禁止 N+1 查询。
6. **口径漂移**：新老接口同时存在时，必须用 parity 测试锁住；板块状态与 `theme_board` 的「新晋/延续/退潮」不是同一概念，UI 文案要区分。
7. **分类体系**：精选板块与行业、同花顺行业、涨停原因不能混排；接口强制 `taxonomy`。

### 14.2 需要用户确认的事项

1. **是否同意把统一回测（P0-5 内核）提前到 B0**，作为 P0-1~P0-4 的上线门槛？
2. **是否批准一次性回补 `sector_member` 60/120 天**（免费接口，但耗时、需外部请求）？若否，P0-1 广度与 P0-4 历史只显示「数据不足」。
3. **是否同意 `RpsDaily` 加 4 个可空列**（自动迁移，不改旧列）？
4. **趋势池默认股票范围**：仅主板+创业板+科创板，还是包含北交所？（建议排除北交所）
5. **默认持有期与基准**：5/10/20/40/60 日、全市场等权；是否需要增加创业板指/中证1000 作为可选基准？
6. **费用默认值**：页面默认 0 bps（观察口径）还是 15 bps（双边粗略）？建议同时展示两套。
7. **状态命名**：是否接受「新晋强势 / 持续走强 / 强势整理 / 强度回落 / 持续弱势 / 数据不足」？
8. **是否允许把「低位启动」状态写入 `pattern_hit` 或独立表**？建议先不落库，按请求计算；若需历史回放再落库。

---

## 15. 交付核对

| 要求 | 本报告 |
| --- | --- |
| 当前 Git HEAD 与工作区状态 | §1.1 |
| 现有功能审查结果 | §2 |
| 已实现/复用/修改/新增清单 | §3、§5.2、§11 |
| 具体修改文件与主要函数 | §5.2、§6~§10 |
| 新增 API 与字段定义 | §6.3、§7.4、§8.4、§9.4、§10.3 |
| 数据来源、历史覆盖、计算口径 | §4、§6.1、§7.1、§8.1、§10.1 |
| 算法验证与历史样本结果 | §0、§4、§10.4（引用现有 `docs/回测_*.md`） |
| 测试命令和实际结果 | §1.2、§12.3（阶段 A 仅跑 2 个文件：9 passed） |
| 已知问题、未完成、风险 | §3、§4.2、§14 |
| 是否涉及生产/部署 | 否；未连接生产、未部署、未改数据库 |

---

**阶段 A 结论**：建议批准进入 B0。B0 不先做页面，而是先把「数据地基 + 统一回测 + 偏差修正」做完；随后 B1~B4 每个 P0 都以「同一参数的候选 + 历史验证」成对交付，确保页面上的每一个状态和候选都能回答「为什么入选、哪些数据支持、哪些条件不满足、何时失效」。
