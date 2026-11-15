# S6.10 P0：数据库与数据安全发布就绪度

- 时间：2026-11-15
- 只读证据：`../protected-assets-before.json`、`../protected-assets-final.json`、
  `../database-integrity-before.txt`、`../gate0-compare.json`
- 迁移证据：`reports/s6-followup/g7/migration-revalidation.json`（本轮 15:44 生成，`status=PASS`）
- **未接触生产主机**：生产库路径、权限、实际备份记录无法在本轮验证，相关项标 `NOT_VERIFIED`。

## 1. 数据库清单与用途（本机基线）

| 数据库 | 用途 | 大小 | integrity | 本轮是否写入 |
| --- | --- | --- | --- | --- |
| `backend/data/fupan.db` | 生产样式的单库（用户 + 研究） | 513,519,616 | `ok` | 否（只读打开） |
| `backend/data/b1_research.db` | B1 研究库（只读验收源） | 1,441,378,304 | `ok` | 否（`mode=ro`，前后 SHA256 一致） |
| `backend/data/history.db` | 历史库 | 869,801,984 | `ok` | 否 |
| `backend/data/forward_research.db` | 前瞻研究库 | 67,055,616 | `ok` | 否 |
| `b0_trial*.db` / `b2_final_snapshot.db` / `p092_*` / `p10_*` | 历史验证副本 | — | 未复查（不在保护清单） | 否 |

G0 前后对照（`gate0-compare.json`）：

```text
all_ok = true
c0_scripts_same = true
frozen_signals_same = true
4 个受保护库 size 与 mtime 完全一致，integrity 均 ok
```

`b1_research.db` 在核心业务验收中全文件 SHA256 前后一致（`../core-business-acceptance.json`）。

## 2. 生产数据库模式（关键差异）

| 项 | 状态 |
| --- | --- |
| `install.sh` 是否开启拆分库 | 否 → 默认 `db_split_enabled=false`，**单库可写** |
| 研究库只读 / 用户库分离 | 仅 Staging 验收开启（`mode=ro` + `PRAGMA query_only`） |
| 生产实际 `.env` 是否覆盖 `DB_SPLIT_ENABLED` | **`NOT_VERIFIED`**（需在主机确认“是否”，不需要回传内容） |

含义：**“研究库只读”不是生产现状**，只是验收环境事实。上线前必须明确生产是否切换到
拆分模式；若继续单库，则 `readiness` 的 `DATABASE_READ_ONLY_VERIFIED` 门禁不适用于生产，
监控与回滚方案要按单库可写设计。

## 3. 读写权限

| 项 | 声明 | 状态 |
| --- | --- | --- |
| 运行用户 | `fupan`（`--system`、`nologin`） | 声明 |
| 数据目录 | `chmod 700 backend/data` | 声明 |
| 代码目录 | 755/644，`.env` 600 | 声明 |
| 生产文件实际权限 | 未采集 | **`NOT_VERIFIED`** |
| 本机（Windows） | 无 POSIX 语义 | 不适用 |

## 4. 迁移兼容性与幂等性

`scripts/s7_migration_revalidate.py`（源库 `fupan.db`，隔离副本，`mode=ro` 打开源库）：

| 检查 | 结果 |
| --- | --- |
| 源库迁移前后 SHA256 | 不变（`b65d59a6…37ee`） |
| 目标库完整性 | `ok` |
| 首次迁移差异 | 无（`first_run_diffs=[]`） |
| 重复执行（幂等） | `idempotent=true` |
| 中断恢复（删部分行后重跑） | `recovered=true` |
| Schema fingerprint | 源/目标一致 |
| 总判定 | **`PASS`** |

启动策略：`schema_mode=verify`（默认）—— 缺表/缺列/缺索引时**启动失败**，
不会隐式 `create_all` 或静默迁移；生产不设 `FUPAN_SCHEMA_MODE` 即采用该策略。

## 5. 本机备份记录（不代表生产）

- 代码逻辑：每日 23:10，`sqlite3.Connection.backup()`（含 WAL），保留 5 份，可选异地目录；
- 本机 `fupan.db` 的 `collect_log` 里 `db_backup` 仅 2026-10-18 的测试行（`test.db-bak-*`），
  **没有任何真实生产备份记录**；
- 本机 `backend/data` 下不存在 `fupan.db-bak-*` 实际备份文件；
- 因此：生产备份是否按日执行、保留策略是否生效、异地同步是否配置 —— **全部 `NOT_VERIFIED`**。

## 6. 恢复能力

独立副本演练（见 `backup-restore-readiness.md`）：

- 源库：`fupan.db`（513 MB 真实数据，`mode=ro`）；
- 备份 → 人为删 3 行 `collect_log` + 1 行 `watchlist` → 恢复；
- 恢复后 8 张关键表的「行数 + rowid 合计 + 前 200 行摘要」与备份前完全一致；
- `integrity_check=ok`，备份文件 SHA256 在恢复前后不变（未被覆盖）。

## 7. 结论

```text
local_db_integrity = PASS (4/4 ok)
protected_db_unchanged = PASS (size/mtime 一致)
migration_compat_idempotent_recoverable = PASS
restore_on_real_copy = PASS
production_db_mode = UNKNOWN (repo default: single writable DB)
production_db_permissions = NOT_VERIFIED
production_backup_history = NOT_VERIFIED
blocking_for_go_live = YES
```

上线前必须在生产主机确认（只读）：

1. `DB_SPLIT_ENABLED` 是否为 true（决定只读模型是否适用）；
2. `ls -l /opt/fupan/backend/data` 与 `df -h`；
3. `journalctl -u fupan | grep db_backup` 最近 7 天是否有成功记录；
4. 备份目录中最近 3 份备份的大小与 mtime；
5. 是否配置 `BACKUP_SYNC_DIR`（异地）。
