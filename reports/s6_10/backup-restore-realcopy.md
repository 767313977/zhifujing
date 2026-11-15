# S6.10 真实库副本备份 / 恢复演练

- 时间：2026-11-15T16:18:13+08:00
- 源库：`C:\Users\76731\Documents\trae_projects\fupan\backend\data\fupan.db`（以 `mode=ro` 打开；大小 513,519,616 字节）
- 环境：临时工作副本 + 临时备份文件；未写入源库
- 备份：online backup API（999.15 ms，514 MB）
- 恢复：backup → 覆盖工作副本（1635.64 ms）

| 检查 | 结果 |
| --- | --- |
| source_read_only_opened | PASS |
| copy_integrity | PASS |
| backup_integrity | PASS |
| backup_matches_source | PASS |
| loss_detected | PASS |
| restore_digest_equal | PASS |
| restore_integrity | PASS |
| backup_file_not_overwritten | PASS |

**结论：PASS**

