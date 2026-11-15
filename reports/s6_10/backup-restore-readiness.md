# S6.10 P0：备份与恢复就绪度

- 时间：2026-11-15
- 证据：`backup-restore-realcopy.json` / `backup-restore-realcopy.md`（本轮真实库副本演练）
- 代码：`backend/app/jobs/backup.py`、`backend/app/jobs/alerts.py`、`config.backup_*`
- 生产主机备份记录/目录：**`NOT_VERIFIED`**

## 1. 备份设计

| 项 | 值 |
| --- | --- |
| 方式 | `sqlite3.Connection.backup()`（在线备份 API，含 WAL） |
| 时刻 | 每日 23:10（晚于 22:30 成分股兜底） |
| 文件名 | `<库名>-bak-<YYYYmmdd-HHMMSS-微秒>` |
| 保留 | 最近 5 份（`backup_keep=5`） |
| 异地 | `BACKUP_SYNC_DIR` 为空时**只在本机同盘** |
| 记录 | 写 `collect_log(task=db_backup, status=ok/failed)` |
| 失败处理 | 记 `failed` 后抛出，调度日志留堆栈；不影响其它任务 |

## 2. 真实库副本演练（本轮）

源库 `backend/data/fupan.db`（513,519,616 字节，`mode=ro` 打开）。
流程：只读源库 → 在线备份生成临时工作副本 → 再备份一份 → 人为丢行 → 用备份恢复。

| 检查 | 结果 |
| --- | --- |
| 源库以 `mode=ro` 打开 | PASS |
| 工作副本 integrity | PASS |
| 备份文件 integrity | PASS |
| 备份内容与源一致（8 表摘要） | PASS |
| 人为丢行被检测到 | PASS（删 3 行 `collect_log` + 1 行 `watchlist`） |
| 恢复后 8 表摘要与原始一致 | PASS |
| 恢复后 integrity | PASS |
| 备份文件未被覆盖（SHA256 不变） | PASS |
| **总判定** | **PASS** |

耗时：复制 1.24s、备份 1.00s、恢复 1.64s（513 MB，本机 SSD）。

对照表（`app_user` / `watchlist` / `review_note` / `user_session` / `collect_log` /
`limit_pool` / `sector_daily` / `stock_daily`）：行数、`SUM(rowid)`、前 200 行内容
SHA256 三项都与备份前一致，不是“只验证文件能打开”。

## 3. 恢复步骤（可审计）

```bash
# 1) 先停服务，避免恢复期间继续写入
sudo systemctl stop fupan

# 2) 再备份当前（可能已损坏的）库，避免二次破坏
sudo -u fupan cp -p /opt/fupan/backend/data/fupan.db \
  /opt/fupan/backend/data/fupan.db-before-restore-$(date +%Y%m%d-%H%M%S)

# 3) 用最近一份备份覆盖（保留备份文件本身，不移动它）
sudo -u fupan cp -p /opt/fupan/backend/data/fupan.db-bak-<时间戳> \
  /opt/fupan/backend/data/fupan.db
sudo -u fupan rm -f /opt/fupan/backend/data/fupan.db-wal /opt/fupan/backend/data/fupan.db-shm

# 4) 完整性 + 摘要核对
sqlite3 /opt/fupan/backend/data/fupan.db "PRAGMA integrity_check;"
sqlite3 /opt/fupan/backend/data/fupan.db \
  "SELECT COUNT(*) FROM app_user; SELECT COUNT(*) FROM watchlist; SELECT COUNT(*) FROM collect_log;"

# 5) 启动并验证
sudo systemctl start fupan
curl -fsS http://127.0.0.1:8000/api/health
```

禁止：在未备份当前库的情况下直接覆盖；禁止用 `mv` 把唯一一份备份移走。

## 4. 保留周期与风险

| 风险 | 说明 | 严重度 | 缓解 |
| --- | --- | --- | --- |
| 只保留 5 份 | 约 7.5 GB（1.5 GB/份），能覆盖“当天中毒、次日发现”，不覆盖更久 | 中 | 需要更长历史就配 `BACKUP_SYNC_DIR` 异地，不要盲目调大 keep |
| 同盘备份 | 未配异地时，盘坏则主库与备份一起丢 | 高 | 上线前必须配置异地目录 |
| 新备份损坏时仍会 prune | `run()` 先备份再 prune；若新备份不完整，旧的好备份可能被清掉 | 中 | 建议上线前加“备份后 integrity_check 通过才 prune” |
| 恢复不自动 | 无自动恢复，需人工执行 | 中 | 恢复步骤已固定；回滚决策人 `PENDING` |
| 告警时点 | 20:00 告警查的是“最近一次”备份；23:10 的失败要次日 20:00 才提示，且非交易日会跳过 | 中 | 增加独立备份失败告警（建议上线前） |
| 生产备份实际状态 | 无法确认最近备份是否成功、是否异地 | 高 | **上线前必须在主机核对 `journalctl` 与备份目录** |

## 5. 结论

```text
backup_integrity_on_real_copy = PASS
restore_digest_equal_on_real_copy = PASS
backup_file_not_overwritten = PASS
retention_policy = 5 (local only unless BACKUP_SYNC_DIR)
offsite_backup_configured = NOT_VERIFIED
production_backup_history = NOT_VERIFIED
backup_failure_alert = PARTIAL (20:00 汇总告警，非即时报错)
blocking_for_go_live = YES (异地备份与生产实际记录需确认)
```
