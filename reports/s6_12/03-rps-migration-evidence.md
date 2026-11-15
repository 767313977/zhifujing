# S6.12 RPS Schema 迁移演练（生产结构副本）

- 时间：2026-11-15T17:23:21+08:00
- 源库：`C:\Users\76731\Documents\trae_projects\fupan\backend\data\fupan.db`（513,519,616 字节，`mode=ro`）
- 副本：临时目录；演练后删除
- 备份 SHA256：`43d1e4ba965940430bf4579e47314533042247e56823122e91265a435051d170`
- 迁移前列：`['trade_date', 'code', 'ret120', 'ret250', 'rps120', 'rps250', 'ret50', 'rps50']`
- 迁移后列：`['trade_date', 'code', 'ret120', 'ret250', 'rps120', 'rps250', 'ret50', 'rps50', 'ret20', 'ret60', 'rps20', 'rps60']`
- 行数/摘要：10206 / `00c6f5d72c6f8aa0…` → 10206 / `00c6f5d72c6f8aa0…`

| 检查 | 结果 |
| --- | --- |
| source_opened_read_only | PASS |
| backup_integrity | PASS |
| backup_old_data_matches | PASS |
| migration_first_exit0 | PASS |
| new_columns_added | PASS |
| old_columns_kept | PASS |
| migration_second_exit0 | PASS |
| idempotent_columns | PASS |
| data_unchanged | PASS |
| integrity_after | PASS |
| interruption_detected | PASS |
| recovery_exit0 | PASS |
| recovery_columns | PASS |
| recovery_data_unchanged | PASS |
| recovery_integrity | PASS |
| legacy_columns_query_ok | PASS |
| new_version_schema_verify_ok | PASS |

**结论：PASS**
