# S6.12 P0-1：研究数据库写入边界

- 时间：2026-11-15
- 目标：普通用户请求不得写研究数据；后台采集仍可写；GET 纯读。
- 约束：继续使用 SQLite 单库，不做分布式/表级账号权限重构。

## 1. 结论

| 项 | 结果 |
| --- | --- |
| 普通用户写 `stock_daily` | **已阻断**（手动同步接口 409） |
| 普通用户写 `sector_member` | **已阻断**（GET 纯读，不再现取落库） |
| 普通用户写 `ifind_usage` | **已阻断**（请求内跳过计量写入） |
| 普通用户写 `stock_event_cache` / `stock_financial` | **已阻断**（请求内只返回数据，不落缓存） |
| 后台采集/CLI 写研究数据 | **保留**（不在 HTTP 请求上下文） |
| 管理员显式写研究数据 | **保留**（`/api/admin/*` 显式放行，仍受管理员校验） |

代码改动：`backend/app/runtime_context.py`、`main.py`、`api/watchlist.py`、
`api/stock.py`、`api/sector.py`、`services/stock_events.py`、`services/financials.py`、
`services/usage.py`、前端 `pages/Watchlist.tsx`（409 提示，不显示为故障）。

## 2. 实现方式（最小边界）

1. 新增请求级上下文 `runtime_context.py`：
   - `http_request_scope(allow_research_write=False)`：HTTP 请求默认禁止研究写入；
   - 后台线程 / 调度器 / CLI 不进入该上下文 → 写入照常；
   - `/api/admin/*` 在中间件里显式 `allow_research_write=True`（管理员操作）。
2. `main.py` 新增 `research_write_boundary` 中间件。
3. 具体写入点：
   - `stock_events._write()`：请求内跳过 `stock_event_cache` 写入；
   - `financials.latest()`：请求内跳过 `stock_financial` 缓存写入（数据照常返回）；
   - `usage.record_call()`：请求内跳过 `ifind_usage` 写入；
   - `api.sector._board_members()`：删除现取/落库分支，只读缓存 + 最近名单回落；
   - 两个 `/sync` 接口：直接 409，不触碰采集器。
4. 明确声明：**SQLite 单库没有表级账号权限**；这里是调用边界 + 中间件 + 负向测试的等效隔离，不是数据库权限。

## 3. 全局写入入口矩阵

| 调用入口 | 身份 | 数据表 | 操作 | 是否允许 |
| --- | --- | --- | --- | --- |
| `POST /api/watchlist/sync` | 普通用户 | `stock_daily`（原） | 采集写 | **禁止（现 409）** |
| `POST /api/stock/{code}/sync` | 普通用户 | `stock_daily`（原） | 采集写 | **禁止（现 409）** |
| `GET /api/sectors/members` | 普通用户 | `sector_member`（原） | 现取写 | **禁止（现纯读）** |
| `GET /api/stock/{code}/events` | 普通用户 | `stock_event_cache` | 缓存写 | 请求内跳过 |
| 个股财务摘要（体检） | 普通用户 | `stock_financial` | 缓存写 | 请求内跳过 |
| iFinD 调用计量 | 任意请求 | `ifind_usage` | 计数写 | 请求内跳过 |
| `POST /api/admin/backfill` | 管理员 | 多张研究表 | 采集写 | 允许（显式） |
| `POST /api/admin/kline/recent` | 管理员 | `stock_daily` | 采集写 | 允许（显式） |
| `POST /api/admin/patterns/scan` | 管理员 | `pattern_hit` | 扫描写 | 允许（显式） |
| 定时任务（15:05 / 17:30 / 19:30 / 22:35 等） | 后台 | 研究表 | 采集写 | 允许 |
| `scripts/collect_*.py` / `scan_*.py` | CLI | 研究表 | 采集写 | 允许 |
| 用户自选/笔记/计划/日志 | 普通用户 | `watchlist` / `review_note` / `trade_*` / `user_risk_pref` | 用户数据写 | 允许（按 user_id 隔离） |
| 登录/会话/邀请码/管理员用户管理 | 任意/管理员 | `app_user` / `user_session` / `invite_code` | 用户数据写 | 允许 |

## 4. 测试（`tests/test_s6_12_write_boundary.py`，7 项全过）

| 测试 | 断言 |
| --- | --- |
| `test_watchlist_sync_rejects_and_writes_nothing` | 409；`stock_daily` 行数不变 |
| `test_stock_sync_rejects_and_writes_nothing` | 409；`stock_daily` 行数不变 |
| `test_sector_members_get_is_read_only` | 采集器被调用即失败；`sector_member` 行数不变 |
| `test_request_cannot_write_event_cache_but_background_can` | 请求内 0 行；后台 1 行 |
| `test_request_cannot_write_usage_but_background_can` | 请求内 0 行；后台 1 行 |
| `test_admin_request_scope_allows_research_write` | 管理员作用域可写 |
| `test_user_a_cannot_delete_user_b_watchlist` | 用户 A 删 B 的自选 → 404，B 行仍在 |

另：隔离 Staging 冒烟（`05-staging-smoke.json`）对 `stock_daily` / `sector_member` /
`ifind_usage` / `stock_event_cache` / `stock_financial` 做前后摘要对比，结果一致。

## 5. 对现有功能的影响

| 功能 | 影响 | 说明 |
| --- | --- | --- |
| 自选股页面「同步」按钮 | 显示提示“手动同步已停用” | 不再报错红条；自选增删改不受影响 |
| 个股页自动补历史 | 软失败（409 被前端忽略） | 首次打开的新票可能暂无本地日线，等每日采集 |
| 板块成分股 | 只读本地缓存；当日未采集时用最近一天名单 + 本地行情 | 不再实时现取 |
| 公告/研报/财务体检 | 数据照常返回，但不写缓存 | 下次打开可能再次请求上游（成本换安全） |
| 每日采集 / 管理员运维 | 不受影响 | 研究数据仍由授权路径写入 |

## 6. 残余风险

- 这是**应用层**边界，不是 OS/数据库权限；直接持有 `.db` 文件的进程仍可写；
- 管理员路径保留研究写权限（显式授权，非普通用户）；
- 若未来新增用户可触发的写研究接口，必须加同样的守卫并补测试。
