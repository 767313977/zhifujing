# S6.11 数据库架构与写入边界

- 时间：2026-11-15
- 生产证据：`production-readonly-probe.json`、`evidence/prod-probe-raw.txt`
- 生产代码：`/opt/fupan`（2026-11-08 部署包，无 `.git`）
- 本地证据：`reports/s6-followup/g7/write-path-inventory.md`、`route-db-ownership.csv`
- **本轮未写生产库、未执行迁移、未修改配置**

## 1. 生产实际架构（OBSERVED）

| 项 | 实测 |
| --- | --- |
| 数据库文件 | `/opt/fupan/backend/data/fupan.db`（1.83 GB）+ WAL 49.5 MB |
| 库数量 | **1** |
| `DB_SPLIT_ENABLED` | 运行时不存在；生产 `config.py` **没有** `db_split_enabled` 字段 |
| 用户库 | 不存在 `user.db` |
| 研究库只读 URI | 不存在 |
| `journal_mode` | WAL |
| 表数量 | 49 |
| 权限 | 文件 600、目录 700、`fupan:fupan` |
| 连接入口（生产代码） | `get_db()` / `session_scope()` —— 同一个可写 engine |

结论：**生产是单库可写，研究数据与用户数据没有物理隔离**。

## 2. 表级归属（按目标版本 G7 清单）

| 域 | 表 |
| --- | --- |
| 研究域（只应由采集/后台写） | `stock_daily`、`index_daily`、`limit_pool`、`lhb`、`sector_daily`、`sector_member`、`market_sentiment`、`pattern_hit`、`rps_daily`、`ifind_usage` 等 37 张 |
| 用户域（允许请求写） | `app_user`、`user_session`、`invite_code`、`watchlist`、`review_note`、`trade_journal`、`trade_t`、`trade_plan`、`user_risk_pref`、`analysis_snapshot` |

生产库里 47 张目标表全部存在，另有 2 张历史表（`etf_share_orphan_backup_20261016`、`sector_fund_flow`）。

## 3. 所有数据库连接入口（生产代码审查）

| 入口 | 形态 | 可写 |
| --- | --- | --- |
| `db.get_db()`（路由依赖） | 单 engine session | **是** |
| `db.session_scope()`（服务/任务） | 单 engine session | **是** |
| `sqlite3.connect`（脚本类） | 直接文件 | 是（本轮未运行） |
| `research_session_scope` / `user_session_scope` | **不存在**（旧版本） | — |
| `require_research_writable()` 守卫 | **不存在**（旧版本） | — |

生产代码中 **没有任何库级只读或写入守卫**；保护只依赖“代码自觉”和目录 700。

## 4. 研究数据写入函数

| 写入点 | 触发方 | 写入表 |
| --- | --- | --- |
| `DailyCollector.sync_stock()`（`collect_daily.py:793`） | `POST /api/stock/{code}/sync`（**普通登录用户**）或每日采集 | `stock_daily` |
| `DailyCollector.sync_watchlist()`（`collect_daily.py:869`） | `POST /api/watchlist/sync`（**普通登录用户**，600s 冷却）或每日采集 | `stock_daily` |
| `services/usage.record_call()`（`usage.py:184`） | 任何 iFinD 调用（含用户手动同步） | `ifind_usage` |
| `api/sector.members()` → `_board_members()`（`sector.py:1225`） | `GET /api/sector/{code}/members` 缓存未命中（**普通登录用户**） | `sector_member` |
| `/api/admin/*`（backfill / kline / patterns） | 管理员 | 多张研究表 |
| 调度任务（15:05 / 17:30 / 19:30 / 22:35 / 22:00 / 22:30 / 09:15） | 服务端 | 全部研究表 + `collect_log` |
| `scripts/collect_*.py` / `scan_*.py` / `backfill_*.py` | 人工 | 研究表 |

## 5. 普通用户能否写研究数据 —— **能**

| 端点 | 鉴权 | 行为 | 证据 |
| --- | --- | --- | --- |
| `POST /api/watchlist/sync` | `current_user`（任意登录用户） | 调 `DailyCollector.sync_watchlist()` → 写入 `stock_daily` | 生产 `api/watchlist.py:281` |
| `POST /api/stock/{code}/sync` | `current_user` | 调 `DailyCollector.sync_stock()` → 写入 `stock_daily` | 生产 `api/stock.py:1260` |
| `GET /api/sector/{code}/members` | `current_user` | 缓存未命中时向开盘红现取并落库 `sector_member` | 生产 `api/sector.py:1225` |

限制只有：iFinD 配额守卫、600s/60s 用户冷却、采集锁、代码存在性检查。
这些是**成本控制**，不是写入边界。

## 6. 采集任务的写入权限

- 调度器与 Web 进程同用户、同 engine、同文件 → 采集任务拥有完整写权限；
- 生产 worker = 1（未用 `--workers`），符合单调度器要求；
- 采集与 Web 共库意味着 **Web 侧一次写锁/长事务会影响采集**（WAL 下读不受阻，写串行）。

## 7. 迁移兼容性（目标版本 vs 生产）

| 检查 | 结果 |
| --- | --- |
| 目标版本要求表 | 47 张，**全部存在** |
| 生产多出表 | 2 张（历史遗留，不影响 `create_all` / verify） |
| **缺失列** | `rps_daily` 缺 `ret20` / `ret60` / `rps20` / `rps60` |
| 迁移脚本 | `scripts/migrate_rps_columns.py`：`ALTER TABLE ... ADD COLUMN FLOAT`（可空），已存在则跳过（**幂等**），可用 `--rollback` 删除 |
| 默认拒绝主库 | 脚本默认拒写 `backend/data/fupan.db`，需显式 `--allow-main-db` |
| 当前直接部署目标版本 | **会失败**（`schema_mode=verify` 检出缺列） |
| 兼容性结论 | **需先执行一次显式迁移；迁移本身可逆、可空列、不重写数据** |

本轮**没有执行迁移**（任务禁止修改生产数据库）。

## 8. 等效数据隔离与写入保护能否证明

| 要求 | 结论 |
| --- | --- |
| 研究库物理只读 | **无** |
| 研究库逻辑只读（`PRAGMA query_only`） | **无** |
| 路由级写入守卫 | **无**（生产代码无 `require_research_writable`） |
| 表级权限（SQLite 无此能力） | **无** |
| 普通用户写入被阻断 | **否**（§5 已证） |
| 结论 | **无法证明等效隔离 → BLOCKED** |

## 9. 最小修复方案（未实施，待审查）

### 方案 A（推荐，改动最小）：独立的“研究库写入禁用”开关

1. 新增配置 `RESEARCH_WRITE_DISABLED=false`；
2. 新增依赖/守卫 `require_research_writable()`：开关为 true 时返回 `503`（固定文案，不含路径）；
3. 挂到会写研究表的用户入口：
   - `POST /api/watchlist/sync`
   - `POST /api/stock/{code}/sync`
   - `GET /api/sector/{code}/members`（缓存未命中时改为“只读缓存 + note”，不落库）
4. 前端对 503 显示“自动同步暂不可用，展示本地缓存”，不伪装成功；
5. 测试：断言 503、断言无 `stock_daily` / `sector_member` 写入、断言缓存命中路径仍 200。

影响：用户首次查看未缓存个股/板块时没有即时数据，需等每日采集；不改变采集任务写入。
回滚：删除开关或置 false。
成本：约 1 个守卫 + 3 个路由依赖 + 4~6 个测试。

### 方案 B（目标架构）：启用拆分库

- 研究库 `mode=ro` + `PRAGMA query_only=ON`，用户库独立；
- 需要：初始化 `user.db`、迁移 10 张用户表、改 systemd 配置、重启；
- 影响大、需要停机窗口与回滚演练，但能从根本上把研究库变只读。
- 目标版本已具备该能力（`DB_SPLIT_ENABLED`），生产尚未启用。

### 方案 C（临时）：nginx 层拒绝同步端点

- 对 `/api/watchlist/sync`、`/api/stock/*/sync` 返回 403/503；
- 只挡两个入口，`/api/sector/*/members` 与 `ifind_usage` 仍可写 → **不充分**，只能作为过渡。

**建议：先 A 止血，再在维护窗口评估 B。**

## 10. 结论

```text
production_db_architecture = SINGLE_WRITABLE (verified)
research_user_isolation = NONE (verified)
normal_user_can_write_research = YES (watchlist sync / stock sync / sector members)
equivalent_write_protection = NOT_PROVEN
schema_compatible_with_target = NO (rps_daily 缺 4 列)
migration_required = YES (additive, idempotent, reversible)
database_write_boundary = BLOCKED
```
