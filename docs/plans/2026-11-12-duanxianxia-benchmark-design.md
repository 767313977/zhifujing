# 短线侠对标与致富经增量改造设计（只读审计）

- 日期：2026-11-12
- 基线：`feature/stock-screening-v2` / `0871345cb8c2eb289be21e177d758860479470d2`
- 状态：`DESIGN_ONLY`；本轮未修改业务代码、未写数据库、未请求外部 API、未改冻结快照
- 参考：`https://duanxianxia.com/`（仅按其公开功能形态对标，不抓取其受限/会员/站内用户数据）

---

## 0. 结论摘要

1. 致富经已经具备短线侠的大部分**底层数据**：竞价快照模型、涨停/炸板池、连板分层与晋级率、板块轮动、候选池次日验证、情绪指标、个股详情与自选。
2. 真正缺的是**面向短线操作的组织方式**：竞价异动分类、逐层晋级漏斗、题材交集检索、全局股票研究侧栏、情绪与次日表现的统一视图。
3. 三个必须先说清的数据事实：
   - 本机 `fupan.db` 的 `auction_snapshot` 为 **0 行**；竞价代码完整但本地无历史数据，且腾讯竞价额在 9:25 结算前恒为 0，**无法回补、无法生成 9:20–9:25 抢筹曲线**。
   - `limit_pool` 有 **28,225 行 / 255 个交易日**，晋级率已有，但没有“逐层家数 + 逐券结果 + 无法验证”结构。
   - `stock_concept` 只覆盖涨停股（4,052 只、361 个概念，最新到 2026-09-28）；`sector_member` 全市场成分快照本机只有 **5 天**（2026-09-18~24）。概念交集搜索的可靠性取决于每日成分快照是否持续采集。
4. **推荐首个最小增量：P0-B 涨停晋级漏斗**。它只读 `limit_pool`/`collect_log`，有 255 天历史可回放，不依赖竞价实时数据，验收最确定。P0-A 竞价异动页面价值更高，但必须先解决“竞价数据从上线日起才有”的采集前提。

---

## 1. A. 短线侠功能与致富经模块对照

| 短线侠能力 | 致富经现有实现 | 页面/路由 | 主要 API | Service | 表 | 数据源 | 已有指标/交互 | 缺口 | 测试 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 竞价异动分类（昨涨停/昨炸板/断板、爆量、抢筹、封单委买） | 竞价明细 + 昨日连板/炸板标签 + 派生指标 | `/auction`（`frontend/src/pages/Auction.tsx:374`） | `GET /api/auction`（`api/auction.py:441`）、`/auction/boards`（:629） | `api/auction.py` 内 `_metrics/_auction_ratios/_cancel_rate` | `auction_snapshot`、`limit_pool`、`stock_daily`、`stock_basic` | 腾讯批量行情 | 竞价涨幅/额/量比/换手/封成比/撤单率/委差/昨日 N 板/炸板/9:31 开盘后涨幅 | 无“按异动类型聚合”的页面/API；本地无竞价数据；无帧级质量日志 | `tests/test_auction.py`（派生指标 12 项）；无页面测试 |
| 竞价承接（9:25→9:31） | 已有 9:25 结果 + 9:31 `post_open_price` | `/auction`、`/premarket` | 同上；`/premarket/summary`（`api/premarket.py:21`） | `services/premarket.py:338` | 同上 | 腾讯 | 竞价涨幅、开盘后涨幅、状态标签（竞价涨停/高开…） | 没有 `pct_change_after_auction` 独立字段/列；9:31 只更新已存在行 | `test_premarket.py:103/120` |
| 涨停晋级漏斗 | 连板分层 + 晋级率序列 + 阶梯板 | `/limit-up`（`LimitReview.tsx:39`） | `/api/limit/ladder-board`（`api/limit.py:281`）、`/limit/promotion`（:504） | `api/limit.py` 内 | `limit_pool`、`limit_reason`、`stock_daily`、`stock_basic` | akshare 东财 push2ex、同花顺原因 | 昨日档→今日晋级/炸板/未晋级；严格 `level+1`；首板行；题材/原因 | 无逐层 `failed/broken/unverifiable` 家数与逐券结果；无数据质量桶 | 无 `/limit/promotion`、`/ladder-board` 直测 |
| 涨停明细/连板高度/炸板 | 已有 | `/limit-up` | `/limit/pool`（:235）、`/limit/themes`（:479） | 同上 | 同上 | 东财、同花顺 | 封单/首封/开板次数/连板/几天几板/炸板池 | “曾触及→收盘封住→连续涨停”未在 UI 强区分；断板靠 `other` | `test_pools.py` 等 |
| 题材交叉检索 | 板块/概念筛选、形态多选交集 | `/sectors`、`/patterns` | `/sectors/members`（`api/sector.py:1224`）、`/patterns/hits` | `services/pools.py`、`services/sector_rps.py` | `sector_daily/member/basic`、`stock_concept`、`stock_basic` | 开盘红、涨停天梯 | 单板块成分、形态交集、行业过滤 | 无跨概念 AND/OR、无来源/更新时间展示、无冲突记录 | `test_sector_*`、`test_stock_themes.py` |
| 全站股票联动 | `StockLink` + `/stock/:code` + 前后翻 | 各列表、`StockDetail.tsx:326` | `/api/stock/{code}` 等 | `api/stock.py`、`services/stock_meta.py` | `stock_daily/basic`、`stock_dde`、`lhb`、`watchlist` | 本地 + iFinD/东财 | 点代码跳个股、★自选、sessionStorage 前后翻 | 无全局选股上下文/研究侧栏；列表页刷新不恢复 | 仅 `stockNav` 相关无页面测试 |
| 情绪周期 | 12 项情绪快照 + 14 个面板 | `/sentiment`（`Sentiment.tsx:109`） | `/market/sentiment`（`api/market.py:391`）等 | `services/sentiment.py` 等 | `market_sentiment` 等 | 本地计算、东财、乐咕、iFinD | 涨停/跌停/炸板/封板率/最高板/涨跌家数/成交额/昨涨停今均涨 | 无“情绪档位→次日个股表现”统一表；无个股入口 | 无 `market_sentiment` 组装直测 |
| 次日表现 | 首板/RPS/板块跟踪、情绪 `yesterday_limit_today_avg` | `/pools`、`/patterns`、`/sentiment` | `/pools/first-board/track`、`/rps/track`、`/sector-rps/track` | `services/pools.py:1028` 等 | `limit_pool`、`stock_daily`、`pattern_hit` | 本地 | 晋级率、开盘溢价、收盘中位、大面、一字 | 与情绪周期未联动；无“昨日首板/二板/炸板”统一次日视图 | `test_pools_track.py` |
| 板块轮动 | 已有完整轮动/资金流/领涨 | `/sectors`（`Sectors.tsx:102`） | `/sectors/rotation`（`api/sector.py:627`）等 | `api/sector.py` | `sector_daily/member/basic`、`stock_dde` | 开盘红、iFinD DDE | 矩阵、强度、轮动、资金流、龙一 | 无概念交集检索；成员快照本机仅 5 天 | `test_sector_rps.py` 等 |

---

## 2. B. 已存在 / 可复用 / 需补充 / 暂不适合

### 已存在，可直接复用

- 竞价：`AuctionSnapshot`（9:25 结果 + `samples` 逐分钟 + `post_open_price`）、`/api/auction` 的派生指标、`/api/premarket/summary` 的状态标签。
- 涨停：`LimitPool(up/broken/down)`、`/limit/pool`、`/limit/ladder-board`、`/limit/promotion`、`/limit/themes`。
- 情绪：`market_sentiment` 12 项、`/market/sentiment` 与 14 个面板、`yesterday_limit_today_avg`。
- 板块：`sector_daily/member/basic`、轮动/资金流/领涨。
- 次日验证：`pools.first_board_track`（晋级率/开盘溢价/收盘中位/大面）、`patterns.track`、`sector_rps.track`。
- 前端：`Panel`、`SortTh`、`StockLink`、`EChart`、`KLineChart`、日期下拉与 URL 参数（`/sectors`、`/patterns` 已有范式）。

### 需要补充

- 竞价异动聚合 API 与页面区块；帧级采集质量日志；`pct_change_after_auction` 展示字段。
- 晋级漏斗 API（逐层 promoted/broken/failed/unverifiable + 逐券）；漏斗图与逐券下钻。
- 题材交集搜索 API（AND/OR、来源/日期、冲突保留）。
- 全局股票研究侧栏与 URL/本地状态同步。
- 情绪与次日表现统一 API（按日、T+1 到齐才计算）。
- 上述能力的后端/前端测试。

### 暂不适合实现

- **9:20–9:25 抢筹曲线/分段量能**：腾讯在 9:25 前不返回累计成交额，`samples` 的 amount 恒 0；只有 0926 帧有值。不得用价格/委差冒充成交额。
- **竞价历史回补**：竞价数据只能从采集上线日起逐日积累，不能从日线反推。
- **全市场概念交集**：在 `sector_member` 每日全市场快照未稳定覆盖前，只能搜索“涨停过的股票”或最近 5 天成员，必须显式标注覆盖率。
- **通达信/同花顺外部联动**：涉及客户端协议、授权与稳定性，先只评估。
- **复制短线侠付费/会员/站内数据**：不做。

---

## 3. C. 优先级与依赖

| 优先级 | 功能 | 依赖 | 是否需新数据 | 是否可历史回放 |
| --- | --- | --- | --- | --- |
| P0-B | 涨停晋级漏斗 + 逐券结果 | `limit_pool`、`collect_log`、`stock_daily` | 否 | 是（255 天） |
| P0-A | 竞价异动与承接 | 竞价快照、帧级质量日志、昨涨停/炸板池 | 是（从上线日起的实时采集） | 否 |
| P1-C | 情绪 × 次日表现 | `market_sentiment`、`limit_pool`、`stock_daily`、`pools` 口径 | 否 | 是 |
| P1-A | 题材交叉检索 | `sector_member` 最新全市场快照、`stock_concept`、`stock_basic` | 否（但需保证每日成员快照） | 部分（受成员快照覆盖限制） |
| P1-B | 全局股票研究侧栏 | 前端上下文 + 现有股票/题材/资金/龙虎榜 API | 否 | 不适用 |
| P2 | 通达信/同花顺联动 | 客户端协议、授权 | 待评估 | 不适用 |

依赖关系：P0-B 独立；P0-A 依赖采集与质量日志；P1-C 可复用 P0-B 的池口径；P1-A 依赖成员快照；P1-B 最后做，避免在页面未稳定时先行重构。

---

## 4. D. 每项功能的真实源码、API、表与数据源

### 4.1 P0-A 竞价异动与承接

**现有结构（已核实）**

- `backend/app/models.py:1294-1341` `AuctionSnapshot`：`price/pct_chg/open/amount/volume/turnover/volume_ratio/float_mv/total_mv/limit_up/limit_down/samples/post_open_price`。
- `samples` 下标 `models.py:1284-1291`：`[HHMM, 价, 累计成交额, 委差, 买一价, 买一量, 卖一价, 卖一量]`；老行只有前 3 项，读端越界当 `None`。
- `backend/app/jobs/collect_auction.py:35-43`：9:15–9:26 每分钟采样 + 9:31；`:121-216` 9:25 结果取 0926 帧，9:31 只更新已存在行的 `volume_ratio/post_open_price`。
- `backend/app/api/auction.py:38-350`：已有 `auction_ratio`、`amplitude`、`free_turnover`、`seal_ratio`、`cancel_rate`、`committee_diff`、行业/全市场排名；`api/auction.py:352-438` 板块概览含昨日涨停/炸板梯队。
- `backend/app/api/premarket.py:21` + `services/premarket.py:117-198`：已有竞价状态标签（高开/低开/竞价涨停/跌停等）。

**数据事实（只读）**

- 本机 `fupan.db.auction_snapshot` = **0 行**；`stock_daily` 有 506 天，无法反推竞价。
- 9:25 前 `amount` 恒 0（`models.py:1299-1303` 注释）；只有 0926 帧有竞价额。
- 因此：竞价额/昨日成交额之比可算；“9:20–9:25 抢筹额曲线”不可算。

**新增 API 草案**

`GET /api/auction/anomaly?date=YYYY-MM-DD&group=prev_limit|prev_broken|prev_broken_board|all`

响应行（在用户字段基础上补齐来源/质量）：

```text
trade_date, stock_code, stock_name, board, concept, concept_source,
yesterday_limit_status(up/broken/none), yesterday_board_height,
auction_pct, auction_amount, yesterday_amount, auction_amount_ratio,
price_0931, pct_0931, pct_change_after_auction,
auction_timestamp, observed_timestamp, data_source,
data_quality_status(VALID/INCOMPLETE/NOT_COLLECTED/SOURCE_UNAVAILABLE/NO_QUOTE),
quality_reason
```

**质量判定**

| 状态 | 条件 |
| --- | --- |
| `VALID` | 9:25 行存在，price/amount/pct 非空；昨日成交额可算；9:31 可选（缺失只影响 `pct_0931`） |
| `INCOMPLETE` | 9:25 行存在但关键字段缺失，或只有 9:31 帧 |
| `NOT_COLLECTED` | 当日无 `auction_snapshot` 行，且无采集记录 |
| `SOURCE_UNAVAILABLE` | 有采集日志但状态 failed/0 行，或行情时间非当日被丢弃 |
| `NO_QUOTE` | 个股停牌/无成交导致无竞价价（不是金额 0） |

- 缺失一律 `None`，前端显示 `—`；**不得写 0**。
- 需要新增帧级采集日志：复用 `collect_log`，建议 `task='auction'`，`message` 记录 `frame_hhmm/rows/dropped_old_quote/status`；当前竞价 job 失败只 warning、不写日志（`collect_auction.py:133-138`），无法区分 `NOT_COLLECTED` 与 `SOURCE_UNAVAILABLE`。

**页面区块**

A. 顶部摘要：竞价总家数、有效家数、竞价额、昨日涨停今日竞价均涨、9:25→9:31 走强/走弱家数、数据质量徽标。
B. 昨日涨停今日竞价：按昨日连板高度分组，列昨日板高、竞价涨幅、竞价额/昨日额、9:31 涨幅、变化、状态。
C. 昨日炸板今日竞价：同上；炸板股无 `stock_concept` 题材（`models.py:394-395` 明确），题材列留空或改用行业，标来源。
D. 竞价额对比：`auction_amount_ratio` 分布/排行，明确“9:25 结算额”。
E. 9:25→9:31：`pct_change_after_auction`，缺失显示 `—`。
F. 风险提示：竞价涨停/跌停、无法买入、数据缺失、仅 9:25 无 9:31。

### 4.2 P0-B 涨停晋级漏斗

**现有实现**

- `backend/app/models.py:234-263` `LimitPool`：`pool_type(up/down/broken)`、`consecutive`、`open_times`、封单/首封。
- `backend/app/api/limit.py:281-...` `ladder_board`：昨日档分组，今日 `up/broken/other`；`:341-348` 严格 `today.consecutive == level+1`。
- `backend/app/api/limit.py:504-592` `promotion`：`PROMOTION_LEVELS=(1,2,3,4)`，只输出 counts/promoted/rates；无 failed/broken/unverifiable。
- 采集：`backend/app/jobs/collect_daily.py:665-738`，`up` 失败即抛，`down/broken` 容忍；`collect_log` 任务 `limit_pool/pool_up/pool_broken`。

**口径（必须固定并互斥）**

以 D-1 涨停池 `up` 的 `consecutive=k` 为基数：

| 桶 | 判定 |
| --- | --- |
| `promoted` | D 日 `up` 且 `consecutive == k+1` |
| `broken` | D 日 `broken`（曾触板未封住），且不是 promoted |
| `failed` | D 日有有效行情，但不在 `up`/`broken`（收盘未涨停，含断板/回落） |
| `unverifiable` | D 日 `limit_pool` 或 `stock_daily` 缺失/采集失败；D-1 池不完整；无行情且无法确认停牌 |

- 有效分母 = `promoted + broken + failed`；`rate = promoted / 有效分母`。
- **不得**用“最高价触板”判成功；只认 `limit_pool` 口径。
- 一字板：`up` 且 `open_times==0` 且开盘即涨停（可用 `limit_pool` + 日线开盘价判断）；仅在展示层标注，不改变晋级判定。
- ST/不同市场涨跌幅：沿用 `limit_rules` 展示涨跌停价；晋级判定仍用上游 `pool_type/consecutive`，不自行重算。
- `unverifiable` 不计入成功率，单独列出原因。

**新增 API 草案**

`GET /api/limit/promotion-funnel?date=YYYY-MM-DD&levels=1,2,3,4&include_stocks=true`

每层返回：`level,label,base,promoted,broken,failed,unverifiable,valid_base,rate,stocks[]`；逐券 `code/name/board/industry/status/reason/consecutive/open_times/seal_amount`。
数据表：`limit_pool`、`stock_daily`、`stock_basic`、`collect_log`（质量）；不新增表，先实时计算。

**页面**

顶部选择日期；中间横向漏斗（首板→2→3→4→5）；每层点击右侧逐券表；底部“不可验证明细”。缺失日期显示“数据未采集/采集失败”，不显示 0%。

### 4.3 P1-A 题材交叉检索

**现有数据边界**

- `models.py:398-417` `StockConcept`：**只覆盖涨停股**（开盘红涨停天梯），本机 4,052 只、361 概念、最新 2026-09-28。
- `models.py:347-370` `SectorMember`：全市场成分快照，本机仅 5 天（2026-09-18~24）；生产需每日 22:00 预取（`jobs/collect_sectors.py` 的 `collect_all_members`）。
- `stock_basic.industry`：同花顺三级行业，覆盖全 A，但只有行业、不是概念。
- `api/stock.py:1175` 的“所属题材”已优先 `sector_member`、退回 `stock_concept`（`api/stock.py:9,1181-1185`）。

**API 草案**

`GET /api/themes/search?q=&concepts=A,B&mode=and|or&board=&market=&min_mv=&max_mv=&recent_limit_days=&min_anomaly_count=&limit=`

返回每条：`code,name,matched_concepts[],each{concept,source,taxonomy,as_of},last_limit_date,last_limit_consecutive,data_date,missing_notes`。
- 交集只在**同一来源、同一 as_of** 内计算；跨来源同名概念不直接合并，保留冲突记录。
- 过滤“最近涨停”读 `limit_pool`；异动次数读现有异动数据（`anomaly`/`limit_pool` 口径需单独定义）。
- 数据覆盖不足时返回 `coverage: {source, as_of, code_count}`，前端必须显示“仅覆盖 X 只/最近 Y 日”。

**数据模型草案（仅在需要持久化来源时）**

```text
stock_theme_tag(
  as_of date, code text, tag text, source text,
  taxonomy text, raw text, confidence text,
  PRIMARY KEY(as_of, code, tag, source)
)
```

首版可先不建表：用 `sector_member` 最新日 + `stock_concept` 最近 20 日拼装；若要做来源冲突审计，再新增该表。

### 4.4 P1-B 全局股票研究侧栏

- 现有 `components/StockLink.tsx:18` 已有“点击代码/★自选”；`lib/stockNav.ts:41` 用 sessionStorage 做前后翻；`StockDetail.tsx:626-651` 左右键翻票。
- 无全局选股上下文；列表页 URL 不带 `stock`。
- 设计：`StockResearchProvider`（React Context）+ URL `?stock=600000` + `localStorage` 记住上次股票；`BroadcastChannel('stock-research')` 或 `storage` 事件做多窗口同步（同源、可解释、不依赖外部软件）。
- 侧栏复用现有 API：`/api/stock/{code}`（概览）、`/api/stock/{code}/daily`/K 线、`/api/stock/themes`、`/api/stock/dde`、`/api/stock/trap`、`/api/lhb?code=`、`/api/watchlist` 备注、`/api/analysis/snapshots`（研究快照）。
- 日期一致性：侧栏的“当日涨停/异动”使用当前页面 `date`；个股 K 线与基本面用自己的最新日期，并标注日期；不把不同交易日拼成一个结论。
- 请求去重：按 `(code, data_date)` 缓存；切换页面不重复请求外部数据；外部数据只来自本地 API。
- 外部联动：通达信/同花顺仅评估（协议、授权、稳定性），不实现。

### 4.5 P1-C 情绪 × 次日表现

**现有**

- `models.py:457-477` `market_sentiment`：涨停/跌停/炸板/封板率/最高连板/涨跌家数/成交额/`yesterday_limit_today_avg`。
- `api/market.py:391` `/market/sentiment`，`services/sentiment.py` 组装。
- `services/pools.py:1028` `first_board_track` 已有次日晋级率/开盘溢价/收盘中位/大面；`relay_temp` 有首板→涨停率。

**新增 API 草案**

`GET /api/sentiment/next-day?start=&end=`

按交易日返回：`limit_up_count, broken_rate, max_consecutive, ladder_width, market_amount, up_count/down_count, prev_first_board_next{promoted_rate,open_premium_mean,close_median,big_loss_rate}, prev_second_board_next{...}, prev_broken_next{...}, data_status`。
- 次日字段只在 D+1 行情/涨停池到齐后计算；未到齐返回 `pending`，不填 0。
- 计算时点：收盘后主链完成后；页面标注 `computed_at` 与数据截止日。
- 只做关联展示，不声称预测能力。

---

## 5. E. 新字段/数据模型草案

1. **竞价帧日志**（推荐复用 `collect_log`，不新增表）
   - `trade_date`, `task='auction'`, `status(ok/partial/failed)`, `rows`, `message{frame_hhmm, dropped_old_quote, elapsed_ms}`, `created_at`。
   - 用途：区分 `NOT_COLLECTED` 与 `SOURCE_UNAVAILABLE`。
2. **晋级漏斗**
   - 首版不建表；按 `limit_pool` 实时计算。若后续需要缓存，可建 `limit_funnel_daily(trade_date, level, base, promoted, broken, failed, unverifiable, valid_base, rate)`，只读派生。
3. **题材来源表**（可选）
   - `stock_theme_tag` 如上；保留 `source/as_of/taxonomy/raw`，用于冲突审计。
4. **C0 隔离**
   - 以上均不进入 `forward_research.db`、不改 `c0_universe_b1_verified.json`、不改三因子权重/Top20/评价器。

---

## 6. F. 前端布局与交互草案

统一复用 `Panel`、`SortTh`、`EChart`、`StockLink`、日期下拉、URL 参数范式；默认桌面宽度，4K 下双栏/三栏扩展。

### 6.1 竞价异动页（`/auction`）

```text
┌ 顶部摘要：有效家数 | 竞价总额 | 昨涨停今竞价均涨 | 9:25→9:31 走强/走弱 | 数据质量 ┐
├ Tabs：昨日涨停 | 昨日炸板 | 昨日断板 | 全部
├ 左：分组表（板高、竞价%、竞价额/昨额、9:31%、变化、状态、质量）
├ 右：选中股票摘要（现有竞价指标 + 9:31 + 风险提示）
└ 底部：数据质量说明（仅 9:25 / 缺 9:31 / 未采集）
```

点击股票 → 全局股票侧栏；点击“质量” → 显示该日采集帧与缺失原因。
刷新：9:25 后一次、9:31 后一次，手动刷新；不轮询。
盘前信息：竞价与 9:31；收盘后信息：完整日线（本页不展示）。

### 6.2 涨停晋级漏斗页（`/limit-up` 内新增区块或独立 tab）

```text
日期 [2026-11-10]   口径说明（promoted/broken/failed/unverifiable）
┌ 漏斗：首板 120 → 二板 18 → 三板 5 → 四板 2 → 五板 0
├ 每层：基数 | 晋级 | 炸板 | 失败 | 不可验证 | 晋级率
└ 点击层 → 逐券表（代码/名称/板/题材/状态/封单/首封/开板/原因）
```

缺失数据：该层显示“不可验证 N”，不把缺失计入失败；整日缺池显示“未采集”。

### 6.3 题材交叉检索（`/sectors` 或新 `/themes` tab）

```text
搜索框 [机器人] [+ 添加概念]  模式 (同时满足/任一满足)
过滤：板块 | 市场 | 市值 | 最近涨停 | 异动次数
结果表：代码 | 名称 | 匹配概念（来源/更新日） | 最近涨停 | 数据日期 | 缺失提示
```

点击概念标签可移除/替换；结果点击进入股票侧栏。

### 6.4 全局股票研究侧栏

右侧 380–440px 抽屉（4K 可固定为第三栏）：

```text
[股票名 代码] [★] [在个股页打开]
Tabs：概览 | K线 | 题材 | 涨停/连板 | 异动 | 资金/龙虎榜 | 备注
```

URL `?stock=600000`；刷新恢复；多窗口通过 `BroadcastChannel` 同步；备注读写 `watchlist.note`（per-stock），不混用 `review_note`（每日复盘）。

### 6.5 情绪 × 次日表现

```text
日期范围 [近60日]  指标切换
表：日期 | 涨停 | 炸板率 | 最高板 | 梯队宽度 | 成交额 | 上涨/下跌 | 昨首板次日 | 昨二板次日 | 昨炸板次日 | 状态
图：情绪温度 vs 次日溢价（双轴，标注只做关联）
```

次日未到齐显示 `pending`，不画 0。

---

## 7. G. 数据源可靠性、许可与成本

| 数据 | 来源 | 现状 | 风险 | 控制 |
| --- | --- | --- | --- | --- |
| 竞价快照 | 腾讯批量行情（`sources/tencent.fetch_spot`） | 代码完整；本机 0 行；9:25 前无累计额 | 只能上线后积累；盘中丢帧；旧快照混入 | 帧级日志、`_is_today_quote` 已有、失败告警；不回补 |
| 涨停/炸板池 | akshare 东财 push2ex | `limit_pool` 28,225 行/255 日 | 上游字段/家数差异；up 失败即抛、broken 容忍 | 采集日志 + 质量桶；不以最高价补判 |
| 涨停原因 | 同花顺 | 16,930 行 | 只覆盖涨停股；口径差异 | 留空不顶替 |
| 板块/成员 | 开盘红 | 板块 255 天；成员本机 5 天 | 免费接口变更、当日发布晚、覆盖面 | 每日 22:00 预取；显示 as_of/覆盖数 |
| 概念 | 开盘红涨停天梯 | 只覆盖涨停股；最新 2026-09-28 | 不能代表全市场 | 明确标注；交集搜索需成员快照 |
| 情绪/宽度 | 本地 + 乐咕 + iFinD | 255 天 | iFinD 配额 | 不新增批量调用 |
| 龙虎榜/资金 | 东财/akshare、iFinD DDE | 已有 | 上游限频 | 复用现有采集 |
| 短线侠 | 第三方站点 | 仅功能参考 | 会员/受限数据/ToS | 不抓取、不复制 |

iFinD 配额：现有月配额 5,452 次，新增页面**不应**引入逐股 iFinD 请求；全部优先本地表。竞价腾讯零配额；开盘红免费但需控制频率。

---

## 8. H. 对现有代码与 C0 前瞻验证的影响

- 本轮及建议的 P0/P1 均为**新增只读 API + 前端区块**，不改现有表结构（除可选日志/缓存表），不回写 `limit_pool`/`auction_snapshot`。
- 不触碰 `backend/data/forward_research.db`、`backend/data/forward_c0/*`、`c0_universe_b1_verified.json`、`forward_track_c0.py` 的评分/评价逻辑。
- 新增指标不进入 C0 评分；若未来纳入，必须新建策略版本与前瞻评估方案，不能复用 `c0-2026-11-11-b1v`。
- 对历史回测：新 API 只读现有表，不改变历史结果；但概念/成员数据的 as_of 会带来幸存者偏差，报告中必须标注。
- 对生产：新增路由需鉴权（沿用全局登录）、限流/缓存；不新增调度任务（除竞价帧日志写入现有 job）。

---

## 9. I. 分阶段、可测试、可回滚的实施计划

### 阶段 1：P0-B 晋级漏斗（推荐先做）

- 后端：新增 `services/limit_funnel.py`（纯函数：分桶/质量判定）+ `api/limit.py` 新端点；复用 `limit_pool/stock_daily/collect_log`。
- 测试：合成 5 类样本（promoted/broken/failed/unverifiable/一字），断言互斥、分母、rate；与 `/limit/promotion` 同日同层 promoted 数一致。
- 前端：`/limit-up` 新 tab；漏斗 + 逐券表 + 日期切换；缺失显示。
- 回滚：新增路由/组件，不动旧接口；关闭 tab 即可。

### 阶段 2：P0-A 竞价异动

- 先补竞价帧日志（`collect_log` task=auction），再新增 `/api/auction/anomaly` 与页面区块。
- 需要至少 1 个交易日的真实竞价数据做验收；无数据时页面显示 `NOT_COLLECTED`，不伪造。
- 测试：质量状态、缺失不填 0、9:31 缺失、竞价额比、昨日分类。
- 回滚：新端点/区块独立；不改变现有 `/auction`。

### 阶段 3：P1-C 情绪 × 次日

- 新增只读 `services/sentiment_next_day.py` 与 API；复用 `first_board_track` 口径；T+1 未到齐返回 pending。
- 测试：T+1 缺失、部分到齐、完整；不得把盘后结果用于盘前。

### 阶段 4：P1-A 题材交集

- 先确认生产 `sector_member` 每日覆盖；新增搜索 API（AND/OR、来源/日期）；可选 `stock_theme_tag`。
- 测试：交集/并集、同名冲突、覆盖不足提示。

### 阶段 5：P1-B 全局股票侧栏

- 前端 Context + URL + BroadcastChannel；复用 `StockLink`；按 `(code,date)` 缓存。
- 测试：URL 恢复、多窗口同步、日期不串用；不新增外部请求。

---

## 10. J. 推荐首先实施的最小增量

**P0-B 涨停晋级漏斗（只读）**

理由：`limit_pool` 已有 255 天数据、现有 `ladder_board/promotion` 可交叉验证、无需竞价实时数据、无需新数据源、失败可回滚；能直接提升复盘效率并暴露“数据不可验证”问题。

### 验收标准

1. `GET /api/limit/promotion-funnel?date=D` 对每个 level 返回 `base/promoted/broken/failed/unverifiable/valid_base/rate`，且 `base = promoted+broken+failed+unverifiable`。
2. `promoted` 与现有 `/api/limit/promotion` 同日同层完全一致（严格 `consecutive == level+1`）。
3. `unverifiable` 不进入分母；页面显示原因（池缺失/日线缺失/采集失败）。
4. 炸板不计入晋级；最高价触板但未封住不得判成功。
5. 日期切换可回放 255 天；缺数据日显示“未采集/不可验证”，不显示 0%。
6. 点击任一层显示逐券明细（代码/名称/板/题材/状态/封单/首封/开板/原因）。
7. 后端单元测试覆盖 5 类样本与边界（base=0、全部不可验证）；全量 pytest 通过。
8. 不写数据库、不改既有接口、不影响 C0/快照；前端无新增外部依赖。

---

## 11. 数据事实与限制清单（供评审）

- `auction_snapshot` 本机 0 行；竞价只能从上线日起采集，9:25 前无累计成交额，不能画抢筹曲线。
- `limit_pool` 28,225 行/255 日；`market_sentiment` 255 日；`stock_concept` 25,382 行/4,052 只/最新 2026-09-28；`sector_member` 14,707 行/仅 5 日。
- `sector_fund_flow` 为孤儿表（有表无写入方），不作为新功能数据源。
- 本机 `review_note` 表结构落后于模型（缺结构化列），侧栏备注应使用 `watchlist.note`，不要依赖 `review_note` 新列。
- 前端 11 个目标页面均无页面级测试；新增功能需补最小交互测试。
