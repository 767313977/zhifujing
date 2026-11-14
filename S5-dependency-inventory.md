# S5 依赖清单与基线核验（Gate 0）

- 生成日期：2026-11-14（Asia/Shanghai）
- 仓库：`c:\Users\76731\Documents\trae_projects\fupan`
- 分支：`feature/stock-screening-v2`
- 基线 HEAD：`395ae3a516572fb3656e0c426da83236338d2965`（S4 验收报告提交），与任务书要求一致
- 工作区：`git status --short --branch` 无改动、无 untracked
- 远端：`origin/feature/stock-screening-v2` 与本地同步
- 回看差异：`git diff 7eaf798..HEAD --name-status` 仅包含 S4 的 6 个提交内容（services / api / schemas / frontend / reports / scripts / tests），
  未包含生产 DB、`forward_c0/*`、C0 五脚本、`deploy/*`、调度配置。

## 1. 冻结与只读基线（本轮复核，未重生成）

### 1.1 C0 五个 Git blob

| 文件 | blob | 与 S4 报告 |
| --- | --- | --- |
| `scripts/forward_collect_tushare.py` | `1408f9e3253f0c353023863821ad70544f0fdf2a` | 一致 |
| `scripts/forward_store.py` | `8064d6709a99b07e853c09c297bf26ad938fab5f` | 一致 |
| `scripts/forward_track_c0.py` | `6ff3eb0861cb52580c2e20be2be778cf5bfb9abe` | 一致 |
| `scripts/forward_universe.py` | `2f8979d299f2537427e3ab3c3031a2cb14d22ca0` | 一致 |
| `backend/app/services/factor_score.py` | `32b6f2ff18373ece44f278a6e76db988385be80f` | 一致 |

### 1.2 两份冻结信号 SHA256

| 文件 | SHA256 | 与 S4 报告 |
| --- | --- | --- |
| `backend/data/forward_c0/signals_2026-11-11.jsonl` | `1F80D1B6785B930FC9A922F027A323A36F5DDCF122D2EC865810F33523F07B37` | 一致 |
| `backend/data/forward_c0/signals_2026-11-11.meta.json` | `44301EB5E00867E4E19D51C7DEA524AF0897CED0E7A5002CAC2F530E3FA2DD50` | 一致 |

### 1.3 三个库只读核验（`mode=ro&immutable=1`）

| 库 | 大小 | mtime | `PRAGMA integrity_check` |
| --- | --- | --- | --- |
| `backend/data/fupan.db` | 513,519,616 B | 2026-10-31 22:26:44 | ok |
| `backend/data/b1_research.db` | 1,441,378,304 B | 2026-11-09 12:23:12 | ok |
| `backend/data/forward_research.db` | 67,055,616 B | 2026-11-11 17:17:58 | ok |

- 三个 `*-wal` 均为 0 字节；`fupan.db-wal` 的 mtime 因 S4 末次只读打开刷新为 2026-11-14 16:55:48，**大小为 0，无数据写入**。
- S5 全程只以 `mode=ro&immutable=1`（或临时库副本）读取；不连接写模式。

## 2. 本轮改动边界（计划）

| 包 | 允许改动 |
| --- | --- |
| A | `backend/app/services/stock_research_context.py`、`backend/app/api/stock.py`（research-context 路由参数）、`backend/app/schemas.py`、`frontend/src/components/StockResearchDrawer.tsx`、`frontend/src/api/types.ts`、`frontend/src/pages/__tests__/StockResearchEvidence.test.tsx`、`tests/test_stock_research_context.py`、`tests/test_s5_pit_times.py`、`reports/s5_pit/*` |
| B | `backend/app/services/data_quality.py`、`backend/app/services/auction_anomaly.py`（只读复用其质量判定）、`backend/app/api/quality.py`、`backend/app/schemas.py`、`frontend/src/pages/DataQuality.tsx`、`frontend/src/api/types.ts`、`tests/test_data_quality.py`、`tests/test_s5_quality_contract.py`、`scripts/check_s5_quality_consistency.py`、`reports/s5_quality/*` |
| C | `backend/app/services/post_market_workbench.py`、`backend/app/api/research.py`、`backend/app/schemas.py`、`frontend/src/pages/PostMarketWorkbench.tsx`、`frontend/src/components/PostMarketWorkbench*.tsx`、`frontend/src/App.tsx`、`frontend/src/components/Layout.tsx`、`frontend/src/api/client.ts`、`frontend/src/api/types.ts`、对应测试、`reports/s5_workbench/*` |
| D | `backend/app/external_guard.py`、`backend/app/main.py`、`tests/test_external_guard.py`、`tests/test_s5_offline_gates.py`、`scripts/benchmark_s5.py`、`reports/s5_security/*`、`reports/s5_browser/*`、`reports/s5_performance/*` |

明确不改：`deploy/*`、调度器、`forward_c0/*`、C0 五脚本、冻结信号、生产库、`backend/app/sources/*` 的采集协议（除非仅为 `ensure_external_fetch` 覆盖缺口且不改协议语义）。

## 3. 现有服务与 API 依赖（S5 复用）

| 能力 | 服务 | API |
| --- | --- | --- |
| PIT 研究证据 v2 | `services/stock_research_context.build` | `GET /api/stock/{code}/research-context` |
| 主题交叉 | `services/theme_intersection.search` | `GET /api/sectors/theme-search` |
| 晋级漏斗 | `services/limit_funnel.*` | `GET /api/limit/promotion-funnel` |
| 情绪次日 | `services/sentiment_next_day.*` | `GET /api/sentiment/next-day` |
| 板块共振 | `services/sector_resonance.build` | `GET /api/sectors/resonance` |
| 数据质量 | `services/data_quality.overview/timeline` | `GET /api/quality/overview`、`/timeline`、`/egress` |
| 竞价异动 | `services/auction_anomaly.*` | `GET /api/auction/anomaly` |
| 离线闸门 | `app/external_guard.py` | `GET /api/quality/egress` |

## 4. 已知时点事实（A 包输入证据）

| 表 | 时间字段 | 语义 | 结论 |
| --- | --- | --- | --- |
| `collect_log` | `created_at`（naive，`models._now()` = `datetime.now()`） | 服务端本地时间；文档约定 Asia/Shanghai | 可按 Asia/Shanghai 解释，但必须记录「naive 约定」并允许降级 |
| `sector_member` | `fetched_at`（naive，同上） | 本站写入时间 | 只证明「本站何时取到」，同日一律 `SAME_DAY_UNKNOWN` |
| `stock_daily` | `trade_date` | 无首次入库时间 | 只能算「日终事实」，不能断言 15:00 已知 |
| `market_sentiment` | `trade_date` | 无首次入库时间 | 同上 |
| `stock_concept` | `trade_date` | 无上游发布时间 | 永远 `POST_CLOSE_UNKNOWN` |
| `limit_pool` | `trade_date` | 无写入时间 | 只能借 `collect_log` 的时点，按来源分别判断 |

## 5. 环境与命令

- Python：`c:\Users\76731\Documents\trae_projects\fupan\.venv\Scripts\python.exe`
- 前端：`frontend` 下 `npm test` / `npm run lint` / `npm run build`
- 浏览器：`C:\Program Files\Google\Chrome\Application\chrome.exe`（CDP）
- 运行后端验收时固定：`EXTERNAL_DATA_FETCH_ENABLED=false`、`SCHEDULER_ENABLED=false`、`WARMUP_PATTERN_CACHE=false`、`FUPAN_SCHEMA_MODE=verify`
