# S3 依赖清单（Gate 0）

- 生成日期：2026-11-14
- 分支：`feature/stock-screening-v2`
- 起始 HEAD：`40b13273bb92ced28e177ee32f4645af65fec7b7`（clean）
- 基线：`251cac4c1eadd3e801c798bb3b1180202b05562d`
- 工程约束：`.github/workflows/ci.yml`：`compileall backend scripts`、`ruff check backend scripts tests`、
  `pytest -q tests`、前端 `npm run build` / `npm run lint` / `npm test`。

## 1. 后端函数—模型—API

| 能力 | 函数 / 类 | 模型 | API |
| --- | --- | --- | --- |
| 题材交叉 | `services/theme_intersection.search` | `SectorBasic`、`SectorMember`、`LimitPool` | `GET /api/sectors/theme-search`（`api/sector.py`） |
| 题材响应 | `schemas.ThemeSearch*` | — | 同上 |
| 情绪次日 | `services/sentiment_next_day.build_next_day`、`_day_samples`、`_stats`、`_summary`、`_bands` | `MarketSentiment`、`LimitPool`、`StockDaily`、`CollectLog`、`TradeCalendar` | `GET /api/sentiment/next-day`（`api/market.py::sentiment_next_day_view`） |
| 晋级漏斗 | `services/limit_funnel.load_funnel_inputs`、`build_promotion_funnel`、`classify_promotion`、`assess_funnel_quality` | `LimitPool`、`StockDaily`、`CollectLog`、`StockBasic`、`StockConcept`、`LimitReason` | `GET /api/limit/promotion-funnel`（既有，S3 不改 API） |
| 个股资料 | `api/stock.py` 的 profile / daily / themes / limit-history / lhb / dde | `StockBasic`、`StockDaily`、`SectorMember`、`StockConcept`、`LimitPool`、`Lhb`、`StockDde` | `/api/stock/{code}/*` |
| 个股依据（S3 新增） | `services/stock_research_context.*` | `LimitPool`、`StockDaily`、`MarketSentiment`、`SectorMember`、`StockConcept`、`CollectLog`、`TradeCalendar`、`StockBasic` | `GET /api/stock/{code}/research-context` |
| 鉴权 | `services.auth.current_user` + `main.require_login` | `AppUser`、`UserSession` | 全站 `/api/*` |
| DB 生命周期 | `db.get_db` / `db.session_scope` | — | FastAPI 依赖 |

## 2. 前端

| 能力 | 文件 | 依赖 |
| --- | --- | --- |
| 题材交叉 UI | `components/ThemeIntersection.tsx` | `api.themeSearch`、`StockLink`、`QuickResearch`、`Panel`、`Segmented` |
| 情绪次日 UI | `components/SentimentNextDay.tsx` | `api.sentimentNextDay`、`EChart` |
| 研究侧栏 | `lib/stockResearchContext.ts`、`lib/StockResearchProvider.tsx`、`lib/stockResearchCache.ts`、`components/StockResearchDrawer.tsx`、`components/StockResearchHost.tsx` | `api.stock*`、`QuickResearch`、URL `?stock=`、BroadcastChannel |
| 依据 Tab（S3 新增） | `components/StockResearchDrawer.tsx` 内 `EvidenceTab` | `api.stockResearchContext` |
| 列表接入 | `pages/Sectors.tsx`、`pages/LimitReview.tsx`、`components/LimitTable.tsx`、`pages/Pools.tsx`、`pages/Patterns.tsx`、`pages/Watchlist.tsx`、`pages/Sentiment.tsx` | `QuickResearch` / `StockLink` |

## 3. 测试映射

| 范围 | 文件 |
| --- | --- |
| 题材交叉（现有） | `tests/test_theme_intersection.py` |
| 情绪次日（现有） | `tests/test_sentiment_next_day.py` |
| 个股依据（S3 新增） | `tests/test_stock_research_context.py` |
| 跨包集成 / 四套 fixture（S3 新增） | `tests/test_s3_integration.py` |
| 前端情绪 | `frontend/src/pages/__tests__/SentimentNextDay.test.tsx` |
| 前端题材 | `frontend/src/pages/__tests__/ThemeIntersection.test.tsx` |
| 前端侧栏 | `frontend/src/pages/__tests__/StockResearch.test.tsx`、`StockResearchDrawer.test.tsx` |
| 前端依据（S3 新增） | `frontend/src/pages/__tests__/StockResearchEvidence.test.tsx` |

## 4. 关键数据字段

- `SectorMember(trade_date, sector_code, code, name, fetched_at)`：无 taxonomy 列，需 join
  `SectorBasic.taxonomy`；成员级无完成日志。
- `SectorBasic(code, name, taxonomy)`：`kph_selected` / `kph_industry`。
- `LimitPool(trade_date, code, pool_type, consecutive, name, pct_chg, price, ...)`：
  `pool_up` / `pool_broken`。
- `CollectLog(trade_date, task, status, rows, ...)`：池可信度证据。
- `StockDaily(trade_date, code, open, close, pct_chg, ...)`：不复权 + 真实涨跌幅。
- `MarketSentiment(trade_date, limit_up_count, broken_rate, max_consecutive, up_count, down_count)`。
- `TradeCalendar(trade_date)`：D±1 的唯一来源。
- `SectorMember.fetched_at` 是**本系统写入时间**，不等于上游发布/当时可知时间。

## 5. 冻结与只读基线

| 文件 | Git blob / SHA256 |
| --- | --- |
| `scripts/forward_collect_tushare.py` | `1408f9e3253f0c353023863821ad70544f0fdf2a` |
| `scripts/forward_store.py` | `8064d6709a99b07e853c09c297bf26ad938fab5f` |
| `scripts/forward_track_c0.py` | `6ff3eb0861cb52580c2e20be2be778cf5bfb9abe` |
| `scripts/forward_universe.py` | `2f8979d299f2537427e3ab3c3031a2cb14d22ca0` |
| `backend/app/services/factor_score.py` | `32b6f2ff18373ece44f278a6e76db988385be80f` |
| `forward_c0/signals_2026-11-11.jsonl` | `1F80D1B6785B930FC9A922F027A323A36F5DDCF122D2EC865810F33523F07B37` |
| `forward_c0/signals_2026-11-11.meta.json` | `44301EB5E00867E4E19D51C7DEA524AF0897CED0E7A5002CAC2F530E3FA2DD50` |

库只读状态：`fupan.db` 2026-10-31 22:26:44（513,519,616B）、`b1_research.db` 2026-11-09 12:23:12
（1,441,378,304B）、`forward_research.db` 2026-11-11 17:17:58（67,055,616B）。S3 全程只读，
不写库、不新增采集任务、不启动调度。
