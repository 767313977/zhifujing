# G7：Research DB 全路径只读 / User DB 独立可写（核心工作包 A）

## 结论

| 项 | 状态 | 证据 |
| --- | --- | --- |
| 全路由 / Service / 后台 / CLI 写路径盘点 | 完成 | [route-db-ownership.csv](./route-db-ownership.csv)、[table-ownership-final.csv](./table-ownership-final.csv)、[write-path-inventory.json](./write-path-inventory.json)、[write-path-inventory.md](./write-path-inventory.md) |
| 研究读路径统一走 Research Session | 完成 | 全部研究域路由 `Depends(get_research_db)`；`tests/test_s7_route_ownership.py` |
| 用户写路径统一走 User Session | 完成 | 用户域/混合域路由 `Depends(get_user_db)`；同上 |
| 无隐藏 fallback / 共享 Session | 完成 | API 层禁止 `SessionLocal()` / `session_scope()` / `get_db`（AST 检查） |
| Research ORM / 原生 SQL / 子进程写入被阻断 | 完成（隔离环境实测） | [readiness-proof.json](./readiness-proof.json)、`tests/test_s7_db_negative_matrix.py` |
| User DB 用户操作正常 | 完成 | 全量 pytest（用户域用例原样通过）+ 元数据写入探针 |
| readiness 按真实连接状态计算 | 完成 | [readiness-proof.json](./readiness-proof.json)：`DATABASE_READ_ONLY_VERIFIED=PASS`，其余门禁各自 FAIL（未硬编码） |
| 迁移校验完整 | 完成 | [migration-revalidation.md](./migration-revalidation.md)：幂等 + 中断恢复 + schema fingerprint |
| 生产 Research DB hash/size/mtime 不变 | 完成 | 迁移只读源库，源 SHA256 前后一致；Gate 0 指纹不变 |
| 新增和既有回归通过 | 完成 | `pytest -q`、`ruff check .`（见 S6.5 证据） |

**G7 状态：`G7_RESEARCH_DB_READONLY_VERIFIED`（应用层 + 隔离副本实测）**。  
OS 层（G3/G4）与 24h soak 不因本工作包而通过，仍见对应证据。

## 1. 本轮代码改动（按批次）

### A1 用户域

- `services/auth.py`：`current_user` → `Depends(get_user_db)`；`require_login` → `user_session_scope()`；
- `api/auth.py`、`api/note.py`：仅注入用户库；
- `api/watchlist.py`、`api/journal.py`、`api/positions.py`、`api/plans.py`、
  `api/risk.py`、`api/analysis.py`、`api/admin.py`：混合域改为**两个 Session 显式注入**；
- 服务层签名显式区分：`positions.create/load`、`trade_journal.load_enriched/stats`、
  `trade_plan.load/create_from_signal/stats/suggestions/brief_rows`、`risk.status`、
  `premarket.summary`、`watch_scope.stock_watch_codes/stock_watch_count`。

### A2 研究域

- `market/limit/themes/pools/patterns/valuation/cb/etf/auction/quality/funds/research/
  sector/stock/anomaly_map` 全部改为 `Depends(get_research_db)`；
- `api/deps.resolve_trade_date` → 研究库；
- `api/sector._board_members`、`api/stock._stock_dde` 的「现取后重读」改为
  `research_session_scope()`，并在灰度形态下拒绝隐式补采（503 / 只回缓存）；
- `api/anomaly_map.build_anomaly_map` 删除内部 `SessionLocal()` 兜底，必须显式传 Session。

### A3 混合 / 管理 / 后台

- 新增 `api/deps.require_research_writable()`：灰度形态下研究库写接口统一 503；
  应用于 `watchlist/sync`、`stock/{code}/sync`、`admin/kline|patterns|backfill`；
- `session_scope()` 改为**Research 写入口**：拆分形态下抛 `ResearchDBReadOnlyError`；
- `services/usage.py` 读取自选改走 `user_session_scope()` + `research_session_scope()`；
- `jobs/collect_daily.sync_watchlist`、`jobs/push_brief`、`services/brief_watchlist`
  显式区分用户库与研究库。

## 2. readiness 真实检查（不再硬编码）

`readiness.database_checks()` 每次请求执行 9 项检查：

1. `SPLIT_ENABLED` 2. `RESEARCH_DB_EXISTS` 3. `PATHS_DISTINCT`（路径 + inode）
4. `RESEARCH_ENGINE_READONLY_URI`（`mode=ro`）5. `QUERY_ONLY_ENABLED`（新连接实测）
6. `READONLY_WRITE_PROBE_BLOCKED`（DDL/DML/写型 PRAGMA 全部被拒）
7. `RESEARCH_FILE_PERMISSION_READONLY`（文件权限）
8. `USER_DB_EXISTS` 9. `USER_DB_WRITABLE`（`BEGIN IMMEDIATE` + DDL 探针回滚）

隔离环境实测结果（[readiness-proof.json](./readiness-proof.json)）：

```text
DATABASE_READ_ONLY_VERIFIED = PASS
database_checks = 全部 true
CANARY_READINESS = FAIL
  reasons: STARTUP_NOT_RECORDED / GUARD_NOT_INSTALLED /
           EXTERNAL_FETCH_ENABLED / NO_ISOLATED_OS_NETWORK_ACCESS
```

即：数据库门禁真实通过，其余门禁按实际状态失败 —— 没有为了「看起来就绪」而伪造。

## 3. 路由矩阵规模

- `/api/*` 路由条目：185 行（按「路由 × 连接域」展开，混合路由两行）；
- `database_domain` 仅取 `RESEARCH_READONLY` / `USER_READWRITE` / `ADMIN_WRITE`；
- 生成脚本：`scripts/s7_route_inventory.py`（可重复执行）。

## 4. 未完成 / 保留项

- G3/G4（OS 层 egress deny）仍未实测：本机无可用独立 VM（见 `reports/s6-followup/s61/`）；
- 24h soak 未执行（见 `reports/s6-followup/s64/`）；
- 生产迁移未执行，只做了隔离副本复核。
