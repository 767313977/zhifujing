# S6.2 数据库访问盘点与拆分基础层

- 起始 SHA：`c6db475b9848fc37e703be0bd3f186c9d20d053e`
- 分支：`release/s6-canary-hardening-v2`
- 方法：全仓库只读检索 + ORM 模型/路由逐条核对（不按模块名猜测）
- 机器可读：`db-table-ownership.csv`、`db-write-paths.json`、`migration-*.json`

## 1. 连接入口

| ID | 位置 | 性质 |
| --- | --- | --- |
| C1 | `backend/app/db.py:48` | 应用主 engine（拆分关闭）；拆分打开后指向只读 Research DB |
| C2 | `backend/app/db.py:74` | `SessionLocal`；新增 `ResearchSessionLocal` / `UserSessionLocal` |
| C3 | `backend/app/services/auth.py:341` | 每个已登录请求开 session，可能写 `user_session` |
| C4 | `backend/app/api/sector.py:1372` | 板块成员现取后再读（独立 session） |
| C5 | `backend/app/api/stock.py:809` | 个股 DDE 现取（独立 session） |
| C6 | `backend/app/api/anomaly_map.py:410` | `session=None` 时自建 `SessionLocal` |
| C7 | `backend/app/jobs/backup.py:41` | 备份用原生连接（同库） |
| C8 | `backend/app/readiness.py:83` | 只读探针 `mode=ro&immutable=1` |

## 2. 表归属

- ORM 表 **47** 张：Research 37 / User 10（逐表见 `db-table-ownership.csv`）。
- 非 ORM 但属于研究库：`data_revision`、`data_revision_meta`、
  `sector_member_backfill`、`sector_member_backfill_run`，以及 24 个修订触发器。
- 生产 `fupan.db` 实际只有 **46** 张表：缺 `user_risk_pref`、`analysis_snapshot`
  （旧库未部署这两张用户表）。迁移工具已按 ORM 模型在 User DB 建空表，并记录
  `source_missing_tables`，不伪造数据。

## 3. 写路径矩阵（摘要）

| 模块 | 表 | 域 | 说明 |
| --- | --- | --- | --- |
| auth | app_user / user_session / invite_code | User | 登录、注册、改密、登出 |
| journal | trade_journal / trade_t | User | 写入同时读研究表（日历/日线） |
| positions | trade_journal | User | 持仓=未平仓交易 |
| watchlist | watchlist + stock_daily/stock_basic | Mixed | 用户写入 + 同步采集写研究表 |
| plans | trade_plan / trade_journal | User | 计划执行会改交易日志 |
| note | review_note | User | 复盘笔记 |
| risk | user_risk_pref | User | 风控偏好 |
| analysis | analysis_snapshot | User | 查票快照 |
| admin（用户） | invite_code / app_user / user_session | User | 管理操作 |
| admin（采集） | stock_daily / pattern_hit / collect_log | Research | 触发采集（灰度禁用） |
| sector（现取） | sector_member / sector_basic | Research | GET 内写研究表 |
| stock（现取） | stock_dde / stock_daily / stock_basic | Research | GET/同步内写研究表 |
| scheduler | user_session | User | 过期会话清理（研究调度器写用户表） |
| collectors | 各类研究表 | Research | 定时采集 |
| usage | ifind_usage | Research | 配额计量 |

## 4. 跨域耦合（必须应用层组装）

| 位置 | 用户表 | 研究表 |
| --- | --- | --- |
| `api/stock.py:442` | watchlist | stock_daily / stock_basic / limit_pool |
| `api/watchlist.py:128` | watchlist | stock_daily |
| `services/premarket.py:307` | watchlist | auction_snapshot / stock_daily / limit_pool / pattern_hit |
| `services/trade_plan.py:645` | watchlist | pattern_hit / stock_daily |
| `services/trade_journal.py:571` | trade_journal / trade_t | stock_daily / limit_pool / market_sentiment / trade_calendar |
| `services/positions.py:70` | trade_journal | stock_daily / cb_daily / etf_bar |
| `services/risk.py:163` | trade_journal | market_sentiment / fear_greed_daily |
| `services/usage.py:287` | watchlist | ifind_usage |

## 5. 本轮已实现（基础层）

| 项 | 内容 | 状态 |
| --- | --- | --- |
| 连接层 | `sqlite_readonly_url()`、`ResearchSessionLocal`、`UserSessionLocal`、`get_research_db`、`get_user_db`、`user_session_scope` | PASS |
| 只读强制 | Research 引擎 `mode=ro` + 每连接 `PRAGMA query_only=ON` | PASS（单测） |
| 表归属 | `app/db_roles.py` 单一事实来源（10 张用户表） | PASS |
| User DB 初始化 | `init_user_db()` 只建 10 张用户表，显式调用 | PASS |
| 迁移工具 | `scripts/s6_migrate_user_db.py`：dry-run/apply、隔离目录、backup API、幂等、核对 | PASS |
| 真实副本迁移 | 513,519,616 B 副本 → User DB：9 行、FK 0、integrity ok、源 SHA256 不变、二次执行 PASS | PASS |
| 负向单测 | 只读拒绝 5 类写、缺库 fail closed、越界路径拒绝、幂等 | 5 passed |

## 6. 尚未完成（G7 不能关闭）

1. **路由层切换**：除 `auth` 等少数模块外，大多数 API 仍用 `get_db`；
   需要按表归属改为 `get_research_db` / `get_user_db`。
2. **跨域服务**：上表 8 处需要在应用层分别取两个 Session（不能跨库 JOIN）。
3. **`require_login`**：每个已登录请求都会写 `user_session`，必须切到 UserSession。
4. **研究引擎写路径**：`sector.members`、`stock.dde`、`stock.sync`、`watchlist.sync`、
   `admin.backfill` 需要在 `EXTERNAL_DATA_FETCH_ENABLED=false` 时直接 503，
   且不得持有可写 Research 连接。
5. **启动流程**：`ensure_schema()` 需分别检查 Research（只读）与 User DB（显式初始化），
   不得自动建 Research 表。
6. **文件系统权限**：生产需要 `chmod 0440` + 独立 OS 用户（脚本待补）。
7. **全量负向测试**：盘上写探测、子进程写、崩溃/磁盘满/锁争用。

因此本阶段状态为 **`S62_FOUNDATION_READY` / `G7_RESEARCH_DB_READONLY_VERIFIED = NOT_VERIFIED`**。
拆分默认关闭（`FUPAN_DB_SPLIT_ENABLED=false`），生产行为未改变。

## 7. 迁移工具用法

```bash
# 只预演（不创建目标）
python scripts/s6_migrate_user_db.py \
  --source-copy /isolated/legacy-copy.db \
  --target-user-db /isolated/user.db \
  --allow-dir /isolated --dry-run

# 实际迁移（可重复执行）
python scripts/s6_migrate_user_db.py ... --apply
```

证据：`migration-dry-run.json`、`migration-report.json`、`migration-report-rerun.json`、
`migration-evidence-summary.json`。
