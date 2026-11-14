# G7：写路径总清单（API / Service / 后台 / CLI / 子进程）

机器可读版本：[write-path-inventory.json](./write-path-inventory.json)。  
逐路由矩阵：[route-db-ownership.csv](./route-db-ownership.csv)（由
`scripts/s7_route_inventory.py` 重新生成）。  
逐表归属：[table-ownership-final.csv](./table-ownership-final.csv)。

## 1. 连接域设计

| 入口 | 用途 | 实现 |
| --- | --- | --- |
| `get_research_db` | 研究域**只读**查询 | `ResearchSessionLocal`（拆分时 `mode=ro` + `PRAGMA query_only=ON`） |
| `get_user_db` | 用户域**可写** | `UserSessionLocal`（独立 `user.db`） |
| `research_session_scope()` | 查询期间另开连接（现取后重读） | 只读、不提交 |
| `user_session_scope()` | 后台/服务写用户表 | 自动提交、异常回滚 |
| `session_scope()` | **Research 写**（采集/CLI） | 拆分形态下直接抛 `ResearchDBReadOnlyError` |
| `require_research_writable()` | 研究库写接口守卫 | 拆分形态下 503 |

`get_db()` 仍保留在 `db.py`，但**没有任何路由使用**；
`tests/test_s7_route_ownership.py` 用 AST 检查禁止新增使用。

## 2. 用户域写路径（灰度下允许）

| 接口 / 服务 | 写入表 | 连接域 |
| --- | --- | --- |
| `POST /api/auth/register` `login` `logout` `PUT /password` | `app_user` `invite_code` `user_session` | `get_user_db` |
| `services/auth.require_login`（每请求） | `user_session`（滑动续期） | `user_session_scope()` |
| `POST/PATCH/DELETE /api/watchlist/*` | `watchlist` | `get_user_db` |
| `PUT /api/note/{date}` | `review_note` | `get_user_db` |
| `POST/PUT/DELETE /api/journal/*` | `trade_journal` `trade_t` | `get_user_db` |
| `POST/PUT/DELETE /api/positions/*` | `trade_journal` `trade_plan` | `get_user_db` |
| `POST/PUT/DELETE /api/plans/*` | `trade_plan` `trade_journal` | `get_user_db` |
| `PUT /api/risk/pref` | `user_risk_pref` | `get_user_db` |
| `POST/DELETE /api/analysis/snapshots` | `analysis_snapshot` | `get_user_db` |
| `/api/admin/invites` `/api/admin/users/*` | `invite_code` `app_user` `user_session` | `get_user_db` |
| `PUT /api/risk/pref` 等读取 | 见 `table-ownership-final.csv` | — |

## 3. 研究库写路径（灰度下阻断）

| 入口 | 阻断方式 |
| --- | --- |
| `POST /api/watchlist/sync` | `require_research_writable()` → 503 |
| `POST /api/stock/{code}/sync` | `require_research_writable()` → 503 |
| `POST /api/admin/kline/recent`、`/patterns/scan`、`/backfill` | `require_research_writable()` → 503 |
| `GET /api/sector/{code}/members`（缓存未命中现取） | `research_writable()` 判定 → 503 |
| `GET /api/stock/{code}/dde`（缓存过期现取） | `research_writable()` 判定 → 只回本地缓存 + 说明 |
| `jobs/*`（采集/扫描/推送） | `scheduler` 关闭 + `session_scope()` 拒绝 + 研究库 `mode=ro` |
| `scripts/collect_*.py`、`scan_*.py`、`backfill_*.py`、`migrate_db.py` | 只读文件 + `mode=ro` 阻断；灰度环境不执行 |
| `services/usage.py`（iFinD 配额记账） | `session_scope()` → `ResearchDBReadOnlyError` |
| `main._warm_pattern_cache`（启动预热） | 只读查询；生产配置关闭 |

## 4. 混合域的显式拆分

| 模块 | Research 读 | User 读写 |
| --- | --- | --- |
| `api/watchlist.py` | `stock_basic` / `cb_basic` / `etf_basic` / `stock_daily` | `watchlist` |
| `api/journal.py` | 交易日历、`stock_basic`、日线、情绪档位、板块 | `trade_journal` / `trade_t` |
| `api/positions.py` | 日线、`stock_basic`、交易日历 | `trade_journal` / `trade_plan` |
| `api/plans.py` | `stock_basic`、`pattern_hit`、日线、交易日历 | `trade_plan` / `trade_journal` |
| `api/risk.py` | `market_sentiment` / `fear_greed_daily` / 题材归属 | `user_risk_pref` / `trade_journal` |
| `api/premarket.py` | 涨停池、竞价、日线 | `watchlist` |
| `api/stock.py`（profile） | 个股全量行情 | `watchlist`（是否已收藏） |
| `api/analysis.py` | `stock_basic` / 日线 / 涨停 / 龙虎榜 | `analysis_snapshot` |
| `api/admin.py` | 采集覆盖 / 日志（`/status`） | 邀请码 / 会员 |
| `services/trade_plan.py` | `_calendar` / `_bars` / `metrics_for` | `TradePlan` / `TradeJournal` |
| `services/trade_journal.py` | `_bars` / `_sentiment_levels` / `_sectors` | `TradeJournal` / `TradeT` |
| `services/positions.py` | `latest_quotes` / `open_basis` / `stock_basic` | `TradeJournal` / `TradePlan` |
| `services/risk.py` | 情绪 / 恐贪 / 题材 | `UserRiskPref` / `TradeJournal` |
| `services/premarket.py` | 行情 / 竞价 / 涨停 | `Watchlist` |
| `services/watch_scope.py` | `stock_basic` | `Watchlist`（两步查询，不跨库 JOIN） |

## 5. 验证

- 静态：`tests/test_s7_route_ownership.py`（路由 dependant 树 + AST）
- 动态：`tests/test_s7_db_negative_matrix.py`（只读 URI / ORM commit / 原生 sqlite3 /
  User DB 可写探针 / readiness fail-closed）
- 迁移：`reports/s6-followup/g7/migration-revalidation.md`
