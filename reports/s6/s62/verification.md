# S6.2 数据库拆分验证

## 1. 状态

**`S62_FOUNDATION_READY` / `G7_RESEARCH_DB_READONLY_VERIFIED = NOT_VERIFIED`**

- 已完成：连接层、表归属、User DB 显式初始化、幂等迁移工具、隔离副本真实迁移、
  只读负向测试。
- 未完成：路由层切换、跨域应用层组装、研究引擎写入口收敛、启动流程双库校验、
  文件系统只读权限、全量崩溃/锁争用测试。
- `FUPAN_DB_SPLIT_ENABLED` **默认 false**，生产行为未改变。

## 2. 实测证据

| 项 | 结果 |
| --- | --- |
| 隔离副本迁移（`fupan.db` 513,519,616 B → User DB） | `verdict=PASS` |
| 用户表 | 10 张；复制 9 行（app_user 1、user_session 6、watchlist 1、review_note 1） |
| 源副本 SHA256 前后 | 完全一致（`43d1e4ba…d170`） |
| 外键检查 / integrity | `foreign_key_errors=0` / `ok` |
| 二次执行（幂等） | `verdict=PASS`，行数不变 |
| 旧库缺表（`user_risk_pref`、`analysis_snapshot`） | 按 ORM 建空表，报告 `source_missing_tables` |
| 只读 URI 写拒绝 | INSERT/UPDATE/DELETE/DDL/PRAGMA 全部 `OperationalError` |
| Research DB 缺失 | 连接直接失败，不自动创建 |
| 越界路径 | 迁移工具拒绝（必须位于 `--allow-dir`） |
| 单元测试 | `tests/test_s6_db_split.py` 5 passed |

## 3. 关闭 G7 还需的改动（逐项）

1. `api/auth.py`、`journal/positions/watchlist/plans/note/risk/analysis/admin(用户)`
   改用 `get_user_db`；
2. 纯研究 GET 改用 `get_research_db`；
3. 8 处跨域服务改为双 Session 应用层组装（见 `db-access-inventory.md` §4）；
4. `require_login` 写 `user_session` 切到 User DB；
5. `sector.members`、`stock.dde`、`stock.sync`、`watchlist.sync`、`admin.backfill`
   在外部取数关闭时 503，且不持有可写 Research 连接；
6. `ensure_schema()` 对 Research 只 verify，对 User DB 仅显式初始化；
7. 部署脚本设置 Research DB 文件权限（`0440` + 独立 OS 用户）；
8. 盘上/子进程/崩溃/磁盘满/锁争用负向测试；
9. 完成后把 `FUPAN_DB_SPLIT_ENABLED=true`、`FUPAN_RESEARCH_DB_READONLY=true`，
   并让 `/api/ready` 的 `DATABASE_READ_ONLY_VERIFIED` 变 PASS。

## 4. 结论

本轮没有关闭 G7，也没有把「连接层可用」冒充「全路径只读已验证」。
