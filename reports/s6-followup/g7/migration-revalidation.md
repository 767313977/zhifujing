# G7：513MB 隔离副本 · 用户表迁移复核

- 生成命令：`python scripts/s7_migration_revalidate.py --allow-dir <TEMP>/s7-migrate-*`
- 证据：`reports/s6-followup/g7/migration-revalidation.json`
- 结论：**PASS**（`idempotent=true`、`recovered=true`、`source_unchanged=true`）

## 1. 源库

| 项 | 值 |
| --- | --- |
| 源文件 | `backend/data/fupan.db`（生产研究库，**只读访问**） |
| size | 513,519,616 字节（与 Gate 0 `protected-assets-before.json` 一致） |
| SHA256（迁移前） | `b65d59a65aaee286f65b91aee70df75ff9b34e83e6c266e69be4c491996337ee` |
| SHA256（迁移后） | 同上（源库未被修改） |
| `PRAGMA integrity_check` | `ok` |

> Gate 0 未对 >200MB 的库计算整文件 SHA256；本轮补算了 fupan.db 的完整 SHA256，
> 作为后续阶段性复核的新基准。生产文件的 size/mtime 仍与 Gate 0 记录一致。

## 2. 迁移方式

1. 以 `mode=ro` 打开源库，用 **SQLite backup API** 生成一致性副本
   （不是 `copy` 文件，避免 WAL 处于中间状态时拷出撕裂快照）；
2. 对副本执行 `scripts/s6_migrate_user_db.py --apply`（只建/写 10 张用户表）；
3. 逐表对比源副本与目标 User DB。

迁移前后 **副本 SHA256 也未变化**，证明迁移只读源、只写目标。

## 3. 逐表校验（不只看总行数）

源库有 8 张用户表；`user_risk_pref`、`analysis_snapshot` 在旧库里不存在，
迁移按 ORM 建**空表**（预期行为，已单独登记）。

| 表 | 行数 | 主键/内容摘要 | 关键字段摘要 |
| --- | --- | --- | --- |
| `app_user` | 1 | 一致 | `username` / `is_admin` / `disabled_at` 一致 |
| `invite_code` | 0 | 一致（空） | — |
| `user_session` | 6 | 一致 | `token_hash` / `user_id` / `expires_at` 一致 |
| `watchlist` | 1 | 一致 | `user_id` / `code` / `note` 一致 |
| `review_note` | 1 | 一致 | `user_id` / `trade_date` / `updated_at` 一致 |
| `trade_journal` | 0 | 一致（空） | — |
| `trade_t` | 0 | 一致（空） | — |
| `trade_plan` | 0 | 一致（空） | — |
| `user_risk_pref` | 0（源缺表） | 目标新建空表 | 不得有行 |
| `analysis_snapshot` | 0（源缺表） | 目标新建空表 | 不得有行 |

校验维度：行数、按主键排序后的**整行内容摘要**、关键字段摘要、
`sqlite_master` 中表/索引/触发器 DDL 的 schema fingerprint。

- 交集表的 schema fingerprint：源 `020e0f106d3930fe…` = 目标 `020e0f106d3930fe…`
- 目标库 `PRAGMA integrity_check`：`ok`
- 研究表**没有**进入 User DB（`s6_migrate_user_db.py` 只创建 10 张用户表）

## 4. 重复执行（幂等）

| 运行 | 退出码 | 与源副本的差异 |
| --- | --- | --- |
| 第 1 次 `--apply` | 0 | 0 项 |
| 第 2 次 `--apply` | 0 | 0 项 |

## 5. 中断恢复

模拟「迁移写了一半」：直接删掉目标库 `watchlist` / `trade_journal` /
`trade_plan` / `analysis_snapshot` 的全部行，再执行一次 `--apply`。

- 恢复运行退出码 0；
- 恢复后与源副本差异 **0 项**（行数、主键摘要、关键字段、schema 全部一致）；
- 源副本 SHA256 前后一致。

## 6. 局限

- 这是**副本**复核，不代表已在生产执行迁移；生产迁移仍需独立变更窗口与审批。
- 中断恢复用「删除目标行」模拟，不是真正的进程 kill；进程级中断的原子性由
  `s6_migrate_user_db.py` 的「先建 schema、再 `INSERT OR REPLACE`、最后核对」流程
  保证，最终以可重跑收敛为准。
