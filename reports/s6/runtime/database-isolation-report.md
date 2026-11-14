# S6-B3/B4 数据库隔离与只读实测

## 1. 结论

| 项 | 状态 | 说明 |
| --- | --- | --- |
| SQLite `mode=ro` 写拒绝 | **PASS** | INSERT/UPDATE/DELETE/CREATE/DROP/PRAGMA/VACUUM 全部 `attempt to write a readonly database` |
| 应用实际使用只读连接 | **FAIL** | 引擎固定 `sqlite:///<path>`（读写），未接入 `mode=ro` |
| 源库未被修改 | **PASS** | 实测前后 size/mtime/SHA256 完全一致 |
| 生产库写入路径 | **未关闭** | 见 §3，用户数据与站点写入共用同一库 |
| G7 Canary 数据库只读隔离 | **FAIL（阻塞）** | 需要「只读研究库 + 独立可写用户库」的拆分 |

证据：`reports/s6/runtime/readonly-db-proof.json`（`verdict=PASS`，源库未变）。

## 2. 只读实测（临时副本，未触碰原库）

对象：`backend/data/forward_research.db`（67,055,616 B）的 SQLite backup 副本。

| 语句 | 只读连接结果 | 可写对照 |
| --- | --- | --- |
| `INSERT` | 拒绝：`attempt to write a readonly database` | 成功 |
| `UPDATE` | 拒绝（同上） | 成功 |
| `DELETE` | 拒绝（同上） | 成功 |
| `CREATE TABLE` | 拒绝（同上） | 成功 |
| `DROP TABLE` | 拒绝（同上） | — |
| `PRAGMA user_version=7` | 拒绝（同上） | — |
| `VACUUM` | 拒绝（同上） | — |
| `PRAGMA integrity_check` | `ok` | — |

- 只读连接使用 `mode=ro&immutable=1` 且 `isolation_level=None`，避免隐式事务掩盖结论。
- 临时副本在脚本结束后删除；源库以 `mode=ro` 打开并被 backup 读取。
- 源库 SHA256 前后一致（`source_unchanged=true`）。

## 3. 写入路径盘点

| 模块 | 写入内容 | 灰度处理 |
| --- | --- | --- |
| `api/auth.py` | 用户、会话、登录限速 | 需要**独立可写**用户库 |
| `api/journal.py` | 交易日志/做 T 记录 | 独立可写用户库或灰度禁用 |
| `api/positions.py` | 持仓 | 独立可写用户库或灰度禁用 |
| `api/watchlist.py` | 自选股 | 独立可写用户库或灰度禁用 |
| `api/plans.py` | 交易计划 | 独立可写用户库或灰度禁用 |
| `api/analysis.py` | 分析记录 | 同上 |
| `api/admin.py` | 管理操作（补数/配置） | **灰度禁用**（管理员专属，且会写库） |
| `jobs/*` + `scheduler` | 采集/备份/清理 | 灰度 `SCHEDULER_ENABLED=false` |
| `db.ensure_schema()` | 迁移/建表 | 生产 `FUPAN_SCHEMA_MODE=verify`，不改库 |

## 4. 依赖与副作用

- WAL/SHM：以 `mode=ro&immutable=1` 打开不会创建 WAL；实测副本 `wal.exists=false`。
- `ensure_schema()` 在 `verify` 模式下只读检查；`migrate` 模式会建表/加列 → 灰度禁用。
- 备份任务（`backup_enabled`）在调度器内，灰度关闭调度器即不运行。
- 异常处理不会自动重建/覆盖数据库（未发现此类代码路径）。

## 5. 必须完成才能过 G7

1. 让引擎支持只读 URL（例如 `sqlite:///file:/path?mode=ro&uri=true`），
   并把 `DATABASE_READ_ONLY_VERIFIED` 的判据从「探针能拒绝」改为「引擎 URL 只读 + 探针通过」；
2. 把用户/会话/日志等写表迁移到独立可写库（或灰度期禁用对应页面）；
3. 在灰度环境实测登录、只读研究接口与写入接口的隔离边界；
4. 重新采集三库保护资产指纹，确认未被改动。

本轮**没有**实施上述拆分（超出「验证现有隔离能力」的范围，且会改动产品行为），
因此如实保留 `DATABASE_READ_ONLY_VERIFIED=FAIL` 与 `S6_CANARY_BLOCKED`。
