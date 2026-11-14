# S4 依赖清单（Gate 0）

- 生成日期：2026-11-14（Asia/Shanghai）
- 分支：`feature/stock-screening-v2`
- 起始 HEAD：`7eaf798d7e3d71d0ed244d5006e20cb719781266`（S3 验收报告提交）
- 代码终稿：`8b110f8e45d25ed4953c9b49e257784794e393c2`
- 工作区：`git status --porcelain` 仅 `backend/app/api/stock.py`、`backend/app/schemas.py`
  两条 **stat-dirty**（`git diff` 为空，内容与 HEAD 一致），无未提交内容、无 untracked。
- 工程约束：`.github/workflows/ci.yml` → `compileall backend scripts`、
  `ruff check backend scripts tests`、`pytest -q`；前端 `npm test` / `npm run lint` / `npm run build`。

## 1. 逐路由：只读 / 隐式外呼 / 写缓存 / 调度关系

> 判据来自源码静态审查，不假设「GET 一定只读」。`SCHEDULER_ENABLED` **只控制定时任务**，
> 对下列按需路由没有任何约束 —— 这正是 S3 浏览器验收曾触发开盘红现取的原因。

### 1.1 纯本地只读（仅 SELECT，不外呼、不写库）

| 路由 | 读取 | 备注 |
| --- | --- | --- |
| `GET /api/health` | 无 | 免登录白名单；`detail=1` 仅内网返回三个 bool |
| `GET /api/premarket/summary` | auction_snapshot / stock_daily / sentiment | 只读 |
| `GET /api/market/overview`、`/sentiment`、`/fear-greed`、`/breadth`、`/relay-temp`、`/futures-basis`、`/erp`、`/nh-nl`、`/obv`、`/etf-net-subscription`、`/margin-temp`、`/industry-breadth`、`/northbound-activity`、`/volatility`、`/dates`、`/index-history`、`/advance-decline` | 各对应日表 | 只读；`/erp` 分位读取时现算 |
| `GET /api/sentiment/next-day` | market_sentiment / limit_pool / stock_daily / collect_log / trade_calendar | S2/S3 只读服务 |
| `GET /api/auction`、`/auction/anomaly`、`/auction/boards` | auction_snapshot + auction 帧日志 | 只读，不补采 |
| `GET /api/limit/pool`、`/limit/ladder-board`、`/lhb`、`/limit/themes`、`/limit/promotion`、`/limit/promotion-funnel(/dates)` | limit_pool / lhb / stock_daily / collect_log | 只读 |
| `GET /api/sectors/ranking`、`/series`、`/heat`、`/rotation`、`/rotation/leaders`、`/fund-flow`、`/fund-flow/history`、`/fund-flow/matrix`、`/compare`、`/theme-search` | sector_daily / sector_member / sector_basic / limit_pool | 只读；`theme-search` 不发外呼、不写库 |
| `GET /api/pools/rps`、`/first-board`、`/first-board/track`、`/sector-rps`、`/sector-rps/members`、`/sector-rps/track`、`/sector-stock`、`/sector-stock/track`、`/rps/track` | stock_daily / sector_* / limit_pool | 只读 |
| `GET /api/research/meta`、`/trend`、`/low-start`、`/sector-state` | 研究表 | 只读 |
| `GET /api/patterns/catalog`、`/preview`、`/hits`、`/track`、`/standing`、`/scorecard`、`/action-summary`、`/track/detail`、`/summary` | stock_daily / pattern_hit 等 | 只读；仅 **进程内** 内存缓存（`api/patterns._TRACK_CACHE`），不写库 |
| `GET /api/stock/risk-badges`、`/{code}`、`/{code}/daily`、`/{code}/chips`、`/{code}/themes`、`/{code}/limit-history`、`/{code}/lhb`、`/{code}/research-context` | stock_basic / stock_daily / sector_member / limit_pool / lhb / collect_log | 只读（`themes` 只读快照 + stock_concept） |
| `GET /api/watchlist` | watchlist / stock_daily | 只读 |
| `GET /api/themes/board` | stock_concept / sector_daily | 只读 |
| `GET /api/valuation`、`/valuation/{code}` | valuation 表 | 只读 |
| `GET /api/cb/screen`、`/radar`、`/by-stock/{code}`、`/{bond_code}` | cb_* | 只读 |
| `GET /api/etf/screen`、`/categories`、`/track`、`/related`、`/{code}` | etf_* | 只读 |
| `GET /api/funds/overview`、`/series`、`/futures-series`、`/etf`、`/etf-industry`、`/institutions`、`/dde` | funds 相关日表 | 只读 |
| `GET /api/anomaly-map`、`/anomaly-map/industry` | stock_daily / sector_member | 只读 |

### 1.2 用户数据读写（写库，但不外呼）

| 路由 | 写入 |
| --- | --- |
| `POST /api/auth/register|login|logout`、`PUT /api/auth/password` | `app_user` / `user_session` |
| `GET/POST/PUT/DELETE /api/watchlist` | `watchlist`（删除/改备注，不外呼） |
| `GET/POST/PUT/DELETE /api/journal*` | 交易日志 / 做 T 记录 |
| `GET/POST/PUT/DELETE /api/positions*` | 持仓 |
| `GET/POST/PUT/DELETE /api/plans*` | 交易计划 |
| `GET/PUT /api/note/{trade_date}` | 复盘笔记 |
| `GET/PUT /api/risk/pref` | 风险偏好 |
| `POST/GET/DELETE /api/analysis/snapshots` | 分析快照（用户主动保存） |
| `GET /api/admin/status`、`/invites`、`/users` 等 | 管理读；邀请码/账号管理写 |
| `POST /api/admin/invites`、`/users/{id}/reset-password`、`/users/{id}/disabled` | 管理写 |

### 1.3 **会隐式外呼外部源**（S4-D 必须逐个在发网前阻断）

| 路由 / 触发点 | 外部源 | 触发条件 | 是否写库 |
| --- | --- | --- | --- |
| `GET /api/sectors/members` → `_board_members` → `SectorCollector.collect_members` | 开盘红（kaipanhong） | `sector_member` 当日无缓存且落在最近 10 交易日窗口；受按用户限流/空结果 TTL 约束 | 是：写 `sector_member` |
| `GET /api/stock/{code}/dde?live=true`（默认） → `_stock_dde` → `collect_stock_dde` | iFinD | 本地 DDE 未覆盖最近交易日；先过配额闸与按用户限流 | 是：写 `stock_dde` |
| `GET /api/stock/{code}/news` → `em_news.fetch_stock_news` | 东方财富 | 进程内缓存 10 分钟未命中 | 否（仅内存） |
| `GET /api/stock/{code}/notices` → `stock_events.load("notices")` → `em_events.fetch_notices` | 东方财富 | 6 小时缓存过期/无缓存 | 是：写 `stock_event_cache` |
| `GET /api/stock/{code}/reports` → `stock_events.load("reports")` → `em_events.fetch_reports` | 东方财富 | 24 小时缓存过期/无缓存 | 是：`stock_event_cache` |
| `GET /api/stock/{code}/unlock` → `stock_events.load("unlock")` → `em_events.fetch_unlocks` | 东方财富 | 24 小时缓存过期/无缓存 | 是：`stock_event_cache` |
| `GET /api/stock/{code}/trap` → `trap.base_check` → akshare 财务/人气 + `fetch_stock_news` + `stock_events.load` | akshare / 东财 | 进程内 10 分钟缓存未命中 | 是：间接写 `stock_event_cache` |
| `POST /api/stock/{code}/sync` → `DailyCollector.sync_stock` | iFinD | 每次调用（受配额闸 + 10 分钟冷却） | 是：写 `stock_daily` |
| `POST /api/watchlist/sync` → `DailyCollector.sync_watchlist` | iFinD | 每次调用（受配额闸 + 冷却） | 是：写 `stock_daily` |
| `POST /api/admin/kline/recent` | iFinD（近端日线） | 手动触发 | 是：写 `stock_daily` |
| `POST /api/admin/patterns/scan` | 可能触达东财/腾讯/iFinD 补日线 | 手动触发 | 是：写形态命中/日线 |
| `POST /api/admin/backfill` | iFinD / 开盘红 | 手动触发 | 是：多表 |
| 定时调度器（仅 `SCHEDULER_ENABLED=true`） | 腾讯 / iFinD / 开盘红 / 东财 | 到点触发 | 是 |

### 1.4 明面上的外部客户端（S4-D 的拦截点）

- `sources/tencent.py`（腾讯）
- `sources/kaipanhong.py`（开盘红 `KaipanhongSource._post` / `board_members`）
- `sources/ifind.py`（iFinD `IfindClient.call` / `_download_csv`）
- `sources/eastmoney.py`、`sources/em_news.py`、`sources/em_events.py`（东财）
- `sources/akshare_source.py`（akshare 内部 requests）
- `sources/ths_limit_up.py`、`sources/etf_exchange.py`、`sources/danjuan.py`、`sources/tdx.py`、`sources/kph_local.py`
- `jobs/push_brief.py`（飞书 webhook POST）

> 这些来源目前**共用 `requests`**，没有统一出口函数；S4-D 在来源入口与按需服务入口加
> `EXTERNAL_DATA_FETCH_ENABLED` 闸门，并在测试进程加 requests 层 egress spy 兜底。

## 2. 服务 / 模型 / API 映射（S4 相关）

| 能力 | 服务 | 模型 | API |
| --- | --- | --- | --- |
| PIT 研究证据 | `services/stock_research_context.build` | LimitPool / StockDaily / MarketSentiment / SectorMember / SectorBasic / StockConcept / CollectLog / TradeCalendar / StockBasic | `GET /api/stock/{code}/research-context` |
| 主题交叉 | `services/theme_intersection.search` | SectorBasic / SectorMember / LimitPool | `GET /api/sectors/theme-search` |
| 晋级漏斗 | `services/limit_funnel.*` | LimitPool / StockDaily / CollectLog / StockBasic / StockConcept / LimitReason | `GET /api/limit/promotion-funnel` |
| 情绪次日 | `services/sentiment_next_day.*` | MarketSentiment / LimitPool / StockDaily / CollectLog / TradeCalendar | `GET /api/sentiment/next-day` |
| S4 新增：数据质量 | `services/data_quality`（待建） | stock_daily / limit_pool / sector_member / market_sentiment / auction_snapshot / trade_calendar / collect_log | `GET /api/quality/overview`、`/api/quality/timeline` |
| S4 新增：板块共振 | `services/sector_resonance`（待建） | SectorMember / SectorBasic / LimitPool / StockDaily / MarketSentiment / CollectLog / TradeCalendar | `GET /api/sectors/resonance` |

## 3. 前端协议

- `frontend/src/api/client.ts`：统一 `request<T>()`，`stockResearchContext(code, asOf)` 已带 `?as_of=`；
  401/403 不缓存、`QuickResearch` 打开侧栏。
- `frontend/src/lib/stockResearchContext.ts` / `StockResearchProvider.tsx` / `stockResearchCache.ts`：
  `?stock=&as_of=` 持久化、BroadcastChannel、按账户清理缓存。
- `frontend/src/components/StockResearchDrawer.tsx`：Tab = 概览 / K线 / 题材 / 涨停 / 资金 / 龙虎榜 / 依据。
- `frontend/src/pages/Sectors.tsx`：`ThemeIntersection` 之后是 S4-C 的挂载点。
- `frontend/src/pages/Settings.tsx` 或独立路由：S4-B 质量台挂载点（待定，按现有导航）。

## 4. 数据库 / 冻结基线（只读核验，仅比较不重生成）

### 4.1 C0 五个 Git blob（与 S3 清单一致）

| 文件 | blob |
| --- | --- |
| `scripts/forward_collect_tushare.py` | `1408f9e3253f0c353023863821ad70544f0fdf2a` |
| `scripts/forward_store.py` | `8064d6709a99b07e853c09c297bf26ad938fab5f` |
| `scripts/forward_track_c0.py` | `6ff3eb0861cb52580c2e20be2be778cf5bfb9abe` |
| `scripts/forward_universe.py` | `2f8979d299f2537427e3ab3c3031a2cb14d22ca0` |
| `backend/app/services/factor_score.py` | `32b6f2ff18373ece44f278a6e76db988385be80f` |

### 4.2 两份冻结信号 SHA256（与 S3 清单一致）

| 文件 | SHA256 | 大小 |
| --- | --- | --- |
| `backend/data/forward_c0/signals_2026-11-11.jsonl` | `1F80D1B6785B930FC9A922F027A323A36F5DDCF122D2EC865810F33523F07B37` | 44,169 B |
| `backend/data/forward_c0/signals_2026-11-11.meta.json` | `44301EB5E00867E4E19D51C7DEA524AF0897CED0E7A5002CAC2F530E3FA2DD50` | 5,368 B |

### 4.3 三个库只读核验（`mode=ro&immutable=1`）

| 库 | 大小 | mtime | `PRAGMA integrity_check` |
| --- | --- | --- | --- |
| `backend/data/fupan.db` | 513,519,616 B | 2026-10-31 22:26:44 | ok |
| `backend/data/b1_research.db` | 1,441,378,304 B | 2026-11-09 12:23:12 | ok |
| `backend/data/forward_research.db` | 67,055,616 B | 2026-11-11 17:17:58 | ok |

三个库的 mtime/大小与 S3 清单逐项一致；`*-wal` 均为 0 字节，`*-shm` 为历史连接残留，
**未由 S4 打开写入**。S4 全程不连接写模式。

## 5. S4 改动边界

- 只改 `feature/stock-screening-v2`；不合并 master、不部署、不启动生产调度、不真实采集。
- 不触碰 C0 五个脚本 / `factor_score.py` / 冻结信号文件。
- 新 API 一律 `current_user` 鉴权 + 只读 SELECT。
- 浏览器验收只用临时 SQLite、临时账户、`SCHEDULER_ENABLED=false`、
  `EXTERNAL_DATA_FETCH_ENABLED=false`、`FUPAN_SCHEMA_MODE=verify`。
