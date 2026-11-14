# S6-E3 数据库只读验证（Staging）

## 1. 结论

| 项 | 结果 |
| --- | --- |
| Staging 使用独立临时库 | PASS：`%TEMP%\s6_staging\data\staging.db`，非生产路径 |
| 生产库被连接 | **否**：Staging 只使用临时副本；生产 `fupan.db` / `b1_research.db` / `forward_research.db` 未打开 |
| SQLite 只读语义 | PASS：临时副本上 INSERT/UPDATE/DELETE/DDL/PRAGMA/VACUUM 全部被拒绝（见 `runtime/readonly-db-proof.json`） |
| 应用引擎只读 | **FAIL**：Staging 引擎仍是 `sqlite:///path`；登录/注册会写 staging 临时库（预期，但证明灰度需要独立可写用户库） |
| 登录写入是否污染研究库 | 否：只在临时 staging 库；`db_written_by_login=true` 已记录在 smoke 证据中 |

## 2. 证据

- `reports/s6/runtime/readonly-db-proof.json`
  - 源库：`backend/data/forward_research.db`（67,055,616 B）
  - `writable_control.all_succeeded=true`（对照，证明语句合法）
  - `readonly_attempts.all_blocked=true`（7 类写全部 `attempt to write a readonly database`）
  - `readonly_integrity_check=ok`
  - `source_unchanged=true`（源库 SHA256 前后一致）
- `reports/s6/staging/smoke-test-results.json`
  - `artifacts.db_before` / `db_after`：登录导致临时库 SHA 变化（预期）
  - 未出现任何指向生产库的路径

## 3. 为什么仍不能过 G7

灰度要求「研究库只读 + 用户数据可写」分离。当前只有一份 `fupan.db`，
用户、会话、日志、自选、计划与全部研究表同库，引擎 URL 也没有 `mode=ro`。
因此 `DATABASE_READ_ONLY_VERIFIED=FAIL`，灰度阻塞；拆分方案见
`runtime/database-isolation-report.md` §5。

## 4. 边界

- 本文件不声称「应用已经只读」；只证明 SQLite 的只读能力 + Staging 没有碰生产库。
- 生产库未连接、未写入、未迁移。
