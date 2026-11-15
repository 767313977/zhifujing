# S6.12 P0-3：生产备份与回滚

- 时间：2026-11-15
- 本轮只读校验 + 升级时按授权创建升级前备份与旧版副本；**未删除/覆盖任何历史备份或主库**。

## 1. 现有生产备份（只读校验）

> 升级执行时另建了一份**升级前备份**：
> `fupan.db-prerps-20261115-174841`，1,834,639,360 字节，
> SHA256 `c6645d8ccd02f84acfc1c620ef9536cf7ff8469fe3caa02be23a66f2979d5a90`，
> integrity_check = ok；未覆盖任何历史备份。

| 项 | 实测 |
| --- | --- |
| 最近备份 | `/opt/fupan/backend/data/fupan.db-bak-20261113-231000-001069` |
| 时间 | 2026-11-13 23:10 |
| 大小 | 1,834,606,592 字节 |
| SHA256 | `c96b29b27b93912b455d9bda1b1e7c0d9a4cc4da8dd3b2564f733f20d2b6efd3` |
| `PRAGMA integrity_check` | **ok** |
| `stock_daily` 行数 | 2,627,342 |
| `limit_pool` 行数 | 31,253 |
| `app_user` 行数 | 5 |
| 最新交易日 | 2026-11-13 |

- 保留 5 份（11-09 ~ 11-13），计划 `mon-fri 23:10`；
- 11-14/15 为周末，未生成备份属正常；
- **未配置异地同步**（应用日志原文：“备份与主库同盘”）。

## 2. 旧版部署完整副本（已保存到本地与生产）

生产侧：`/opt/fupan-prev-20261115-1800`（345 文件，7.9 MB，`main.py` 与升级前一致）；
本机侧：

| 项 | 值 |
| --- | --- |
| 文件 | `dist-deploy/rollback-prod-20261115.tar.gz` |
| 大小 | 1,541,504 字节 |
| 文件数 | 216 |
| SHA256 | `6a3778862b61285692a67f73676b55d076fc5a07f6ee67c68296ad781a370e25` |
| `backend/app/main.py` SHA256 | `04cb2afa6d7e29f0cec20ce9cd1ee7913d28c2f8274aeb3492e24f4248a1a220` —— **与生产一致** |

同时保存：

| 文件 | 内容 |
| --- | --- |
| `prod-config/fupan.service.txt` | `systemctl cat fupan`（含非敏感 Environment） |
| `prod-config/nginx-T.txt` | `nginx -T` 全量配置 |
| `prod-config/pip-freeze.txt` | 生产 venv 依赖清单 |

未保存 `.env`（密钥留在服务器，避免扩散）；恢复时 `.env` 不需要变。

## 3. 新版本发布包（本地）

| 项 | 值 |
| --- | --- |
| 文件 | `dist-deploy/fupan-2026-11-15.tar.gz` |
| 大小 | 1,913,389 字节 |
| 文件数 | 304 |
| SHA256 | `447af53308b081001c99779e911cd0e3e75da13dd3f362b0d0f9b12a5d755f55` |
| 内容校验 | 含 `backend/app/runtime_context.py`；**不含** `.env`、`fupan.db.seed` |

## 4. 回滚命令（人工，待授权）

```bash
# 1) 停服务
sudo systemctl stop fupan

# 2) 用本地副本回传（示例；文件先从本机 scp 到 /tmp）
sudo mkdir -p /opt/fupan-rollback-20261115
sudo tar xzf /tmp/rollback-prod-20261115.tar.gz -C /opt/fupan-rollback-20261115
sudo rsync -a --delete /opt/fupan-rollback-20261115/ /opt/fupan/

# 3) 保持 .env 与数据库不动（本副本未包含它们）
sudo systemctl start fupan
curl -fsS http://127.0.0.1:8000/api/health
```

回滚后：

- 数据库新增的 4 个可空列**保留**，旧版本忽略它们；
- 若部署时已执行 RPS 迁移，不需要回滚数据库结构；
- 不自动恢复数据库；只有确认数据损坏时才按现有 11-13 备份人工恢复。

## 5. 数据库恢复（仅在确认损坏时）

```bash
sudo systemctl stop fupan
sudo -u fupan cp -p /opt/fupan/backend/data/fupan.db \
  /opt/fupan/backend/data/fupan.db-before-restore-$(date +%Y%m%d-%H%M%S)
sudo -u fupan cp -p /opt/fupan/backend/data/fupan.db-bak-20261113-231000-001069 \
  /opt/fupan/backend/data/fupan.db
sudo -u fupan rm -f /opt/fupan/backend/data/fupan.db-wal /opt/fupan/backend/data/fupan.db-shm
sqlite3 /opt/fupan/backend/data/fupan.db "PRAGMA integrity_check;"
sudo systemctl start fupan
```

禁止覆盖唯一有效备份；恢复前必须先保存当前库。

## 6. 结论

```text
latest_backup_verified = YES (integrity ok, SHA256 recorded)
backup_offsite = NO (deferred, single-disk)
rollback_bundle_saved = YES (216 files, SHA256 recorded, main.py matches production)
config_snapshot = YES (systemd/nginx/pip freeze)
production_files_created_or_deleted_this_round = NO
```
