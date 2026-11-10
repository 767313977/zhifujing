# 简化后的上线及回滚操作清单

> 适用：单机 systemd + SQLite + 单进程。
> 每条命令标注**执行主体 / 路径 / 影响范围 / 回滚方法 / 预期输出**。
> 变更窗口：**不得按「周末」或固定时段预先推定**。部署前必须临场核查①当时的定时任务清单、②当时的数据库写入者，确认全部避开《03》G6 列出的时段且无活动写入后再执行。**「23:00 后」不是安全窗口**——23:10 有数据库备份任务。全程约 40 分钟。

---

## 1. 上线前 30 分钟检查

| 步骤 | 执行主体 | 命令 | 路径 | 影响范围 | 回滚 | 预期输出 |
| --- | --- | --- | --- | --- | --- | --- |
| 0-1 | `[运维]` | `systemctl is-active fupan` | 生产 | 只读 | 无需 | `active` |
| 0-2 | `[运维]` | 按 G1 公式计算：`DB_SIZE=$(stat -c%s /opt/fupan/backend/data/fupan.db); NEEDED=$((DB_SIZE*3 + DB_SIZE + 52428800)); df -B1 /opt /backup`（含 WAL 峰值 = DB_SIZE） | 生产 | 只读 | 无需 | `/opt` 与 `/backup` 可用空间满足《03》G1 分挂载点公式。**不得为压 WAL 而在线执行 `wal_checkpoint(TRUNCATE)`**；WAL 峰值靠预留空间承受 |
| 0-3 | `[运维]` | `sudo -u fupan ps -o pid,rss,cmd -C python` | 生产 | 只读 | 无需 | 记录 RSS 基线 |
| 0-4 | `[运维]` | `ls -lh /opt/fupan/backend/data/fupan.db*` | 生产 | 只读 | 无需 | 确认 `-wal` / `-shm` 现状 |
| 0-5 | `[研发]` | `python scripts/release_manifest.py` + `verify_release_package.py` | 本地 | 只读 | 无需 | 包哈希与清单一致 |
| 0-6 | `[运维]` | 通知使用人：即将维护 30 分钟 | — | — | — | 已通知 |

---

## 2. 备份（服务仍在运行）

| 步骤 | 执行主体 | 命令 / 动作 | 影响范围 | 回滚 | 预期输出 |
| --- | --- | --- | --- | --- | --- |
| 1-1 | `[运维]` | 在线备份（`Connection.backup`，**不要用 `cp`**）：<br>`/opt/fupan/.venv/bin/python - <<'PY'`<br>`import sqlite3,pathlib,datetime`<br>`s=pathlib.Path('/opt/fupan/backend/data/fupan.db')`<br>`d=pathlib.Path('/backup')/f"fupan-pre-{datetime.datetime.now():%Y%m%d-%H%M%S}.db"`<br>`a=sqlite3.connect(f'file:{s}?mode=ro',uri=True); b=sqlite3.connect(d)`<br>`a.backup(b); b.close(); a.close(); print(d)`<br>`PY` | 只读源库，写 1 个新文件 | 删除该文件 | 打印备份路径 |
| 1-2 | `[运维]` | `sqlite3 <备份> "PRAGMA quick_check;"` | 备份文件 | 删除重做 | `ok` |
| 1-3 | `[运维]` | `sha256sum <备份>` 并记入 `backup_meta.json` | 只读 | 无需 | 64 位十六进制 |
| 1-4 | `[运维]` | `tar -czf /backup/fupan-code-$(date +%Y%m%d-%H%M%S).tgz -C /opt --exclude=fupan/.venv --exclude=fupan/backend/data fupan` | 打包旧代码（**不含库与虚拟环境**） | 删除该 tgz | 生成 tgz，大小约数 MB |

**通过条件**：1-2 `ok` 且 1-4 的 tgz 存在。缺任一条，停止上线。

---

## 3. 部署与迁移

| 步骤 | 执行主体 | 命令 / 动作 | 影响范围 | 回滚 | 预期输出 |
| --- | --- | --- | --- | --- | --- |
| 2-1 | `[运维]` | `systemctl stop fupan` | 服务停止 | `systemctl start fupan` | 服务 inactive |
| 2-2 | `[运维]` | `ls -l /opt/fupan/backend/data/fupan.db-wal` | 只读 | 无需 | 正常关闭后应为 0 字节或不存在 |
| 2-3 | `[运维]` | 将发布包解压覆盖到 `/opt/fupan`（**不要解压出 `backend/data/*.db`**） | 仅代码与前端产物 | 用 1-4 的 tgz 回滚 | 文件时间更新 |
| 2-4 | `[运维]` | `/opt/fupan/.venv/bin/python /opt/fupan/scripts/migrate_db.py --db /opt/fupan/backend/data/fupan.db --status` | 只读 | 无需 | 输出待补表/列/索引数量 |
| 2-5 | `[运维]` | 同上加 `--apply --allow-main-db --backup /backup/fupan-premig-$(date +%Y%m%d-%H%M%S).db` | **仅主库结构**，数据不变 | 用即时备份覆盖 | 退出码 0，迁移成功 |
| 2-6 | `[运维]` | 再跑一次 `--status` | 只读 | 无需 | 全部 `0`（幂等确认） |
| 2-6b | `[运维]` | **安装触发器（主库自动严格）**：`/opt/fupan/.venv/bin/python /opt/fupan/scripts/install_data_revision.py --db /opt/fupan/backend/data/fupan.db --allow-main-db` | 仅新增 `data_revision` 表与触发器，不改业务数据 | `--uninstall` 可移除 | 输出 `db_uuid=...` + `触发器严格校验通过：8 个数据域 / 24 条`；**若不是 24 条或不是「严格校验通过」，立即停止** |
| 2-6c | `[运维]` | 验证触发器：`sqlite3 /opt/fupan/backend/data/fupan.db "SELECT name, tbl_name FROM sqlite_master WHERE type='trigger' ORDER BY name;"` | 只读 | 无需 | 恰 24 条，名称与绑定表同《03》G2 清单；不足 24 条视为未通过 |
| 2-7 | `[运维]` | `bash /opt/fupan/deploy/install.sh` | 依赖、权限、systemd、前端清理，最后 restart | 见第 4 节 | 末尾 `active (running)` |
| 2-8 | `[运维]` | `curl -fsS http://127.0.0.1:8000/api/health` | 只读 | 无需 | HTTP 200 |

**注意**

- `install.sh` **在普通升级（不带 `--with-db` seed）时不会备份数据库**；它只在 `--force-db` 种子替换场景下用 `cp` 备份。因此 1-1 的在线备份是**唯一**的可信备份，不可省略。
- 迁移必须在 `install.sh` 之前完成：新代码 `schema_mode=verify`，schema 不全会直接启动失败（这是设计上的 fail-closed，不要关掉）。
- 若依赖无变化，可跳过 2-7，改为 `systemctl start fupan`。
- 变更窗口必须临场核查当时的定时任务与数据库写入者，并避开《03》G6 列出的全部时段（含 23:10 备份）；「23:00 后」与「周末」都不构成预设的安全窗口。

---

## 4. 上线后验证（15 分钟）

| 步骤 | 执行主体 | 检查 | 通过标准 |
| --- | --- | --- | --- |
| 3-1 | `[运维]` | `systemctl status fupan --no-pager` | `active (running)`，重启次数未增长 |
| 3-2 | `[运维]` | `journalctl -u fupan -n 100 --no-pager` | 无 `Traceback` / `RuntimeError` / `schema` |
| 3-3 | `[运维]` | `SELECT COUNT(*) FROM stock_daily;` 与迁移前对比 | 行数一致 |
| 3-4 | `[运维]` | `SELECT COUNT(*), COUNT(plan_id) FROM trade_journal;` | `(2, 0)`，未被改动 |
| 3-5 | 使用人 | 行情 / 板块 / RPS / 研究 / 登录与自选 | 5 项均正常，无跨用户数据 |
| 3-6 | `[运维]` | 观察 10 分钟 RSS | ≤ 1.5 GB（空闲） |
| 3-7 | `[运维]` | 次日检查 `collect_log` | 无重复任务、无连续锁冲突、无 OOM |

---

## 5. 回滚

### 5.1 代码回滚（最常用，数据保留）

**保护清单（rsync 必须排除）**：`.env`（密钥）、`.venv`（依赖）、`backend/data`（数据库与运行时日志）。这三类**不在旧代码包里**，若被 `--delete` 误删将导致无法启动或丢密钥。

| 步骤 | 执行主体 | 命令 | 影响范围 | 预期输出 |
| --- | --- | --- | --- | --- |
| R-1 | `[运维]` | `systemctl stop fupan` | 服务停止 | inactive |
| R-2 | `[运维]` | `mkdir -p /tmp/fupan_old && tar -xzf /backup/fupan-code-<ts>.tgz -C /tmp/fupan_old` | 临时目录 | 解包成功 |
| R-3 | `[运维]` | **先 dry-run 列出将变更的文件**：`rsync -an --delete --exclude=.env --exclude=.venv --exclude=backend/data /tmp/fupan_old/fupan/ /opt/fupan/` | 只读，不写盘 | 输出将删除/更新的文件列表；**确认列表中无 `.env`、`backend/data/`、`.venv/`** |
| R-4 | `[运维]` | 确认无误后执行：`rsync -a --delete --exclude=.env --exclude=.venv --exclude=backend/data /tmp/fupan_old/fupan/ /opt/fupan/` | 仅代码，**不碰库、venv、.env** | 无报错 |
| R-5 | `[运维]` | 回滚后保护项核验：`ls -l /opt/fupan/.env /opt/fupan/backend/data/fupan.db` | 只读 | 两文件仍存在 |
| R-6 | `[运维]` | `systemctl start fupan && sleep 5 && curl -fsS http://127.0.0.1:8000/api/health` | 服务 | `active` + 200 |
| R-7 | `[运维]` | `rm -rf /tmp/fupan_old` | 临时目录 | 清理完成 |

**说明**：本次迁移只做「加表 / 加列 / 加索引」，旧代码会忽略新增结构，因此**代码回滚通常不需要回滚数据**。优先评估代码回滚，只有数据被破坏时才走 5.2。

### 5.2 数据回滚（仅当数据被破坏 / 校验不一致）

**原则**：先保全现状、再验证备份、在独立位置准备恢复库、最后切换。**不直接覆盖**当前库，不盲删 WAL/SHM。数据回滚 = 丢失备份点之后的写入（当天的采集/自选变动），执行前必须确认这个代价可接受。

| 步骤 | 执行主体 | 命令 | 影响范围 | 预期输出 |
| --- | --- | --- | --- | --- |
| D-1 | `[运维]` | `systemctl stop fupan` | 服务停止 | inactive |
| D-2 | `[运维]` | **确认无其他写入者**：`fuser -v /opt/fupan/backend/data/fupan.db` 或 `lsof /opt/fupan/backend/data/fupan.db` | 只读 | 无进程占用 |
| D-3 | `[运维]` | **保全现状**（不删除）：`TS=$(date +%Y%m%d-%H%M%S); for f in fupan.db fupan.db-wal fupan.db-shm; do [ -f "$f" ] && mv "$f" "${f}.broken-${TS}"; done` | 将当前库与边车改名保留 | 原路径下三个文件均不存在 |
| D-4 | `[运维]` | **验证备份本身完整**：`sqlite3 /backup/fupan-pre-<ts>.db "PRAGMA quick_check;"` | 只读 | `ok`（若不是 `ok`，换另一份备份，不继续） |
| D-5 | `[运维]` | **在独立位置准备恢复库**（在线备份 API，不直接 `cp` 覆盖）：<br>`/opt/fupan/.venv/bin/python -c "import sqlite3,pathlib;s=pathlib.Path('/backup/fupan-pre-<ts>.db');d=pathlib.Path('/opt/fupan/backend/data/fupan.db.restore');a=sqlite3.connect(f'file:{s}?mode=ro',uri=True);b=sqlite3.connect(d);a.backup(b);b.close();a.close();print(d)"` | 新建 `fupan.db.restore`，不碰其他文件 | 打印路径 |
| D-6 | `[运维]` | 核验恢复库完整性与行数：`sqlite3 /opt/fupan/backend/data/fupan.db.restore "PRAGMA quick_check; SELECT COUNT(*) FROM stock_daily; SELECT COUNT(*),COUNT(plan_id) FROM trade_journal;"` | 只读 | `ok` + 行数与备份点一致 + `(2,0)` |
| D-7 | `[运维]` | 设置权限：`chown fupan:fupan /opt/fupan/backend/data/fupan.db.restore && chmod 600 /opt/fupan/backend/data/fupan.db.restore` | 仅该文件权限 | 属主 `fupan:fupan`、mode 600 |
| D-8 | `[运维]` | **切换**（同卷原子改名）：`mv /opt/fupan/backend/data/fupan.db.restore /opt/fupan/backend/data/fupan.db` | 仅文件名变更 | `fupan.db` 存在，`fupan.db.restore` 不存在 |
| D-9 | `[运维]` | 确认无遗留边车：`ls /opt/fupan/backend/data/fupan.db-wal /opt/fupan/backend/data/fupan.db-shm 2>&1` | 只读 | 「No such file」（恢复库是干净单文件，启动时按需重建边车） |
| D-10 | `[运维]` | `systemctl start fupan` 并重跑 3-3 / 3-4 | 服务 | 行数恢复、`(2,0)` |

**保留的 `*.broken-<TS>` 文件**：确认回滚成功且运行 24 小时无异常后，再单独批准删除；在此之前不得清理。

### 5.3 失败矩阵

| 现象 | 判断 | 动作 |
| --- | --- | --- |
| 启动即退出，日志含 `schema` | 迁移未完成或失败 | 回到 5.1 代码回滚，重做 G2/G3 |
| 页面 200 但数据为空 | 连错库 / 备份未落地 | 检查 `backend/data/fupan.db` 大小与路径 |
| 采集重复推送 | 多进程（workers > 1） | 改回单进程，重启 |
| 内存被打满 / OOM | 研究并发或采集峰值 | 回滚代码；临时把缓存 16→8、并发 2→1 |
| 备份任务连续失败 | 磁盘满 / 权限 | `df -h`，修复后手工重跑备份 |
| 登录串数据 | 用户隔离回归失败 | **立即停止对外访问**，代码回滚，排查 |

---

## 6. 明确禁止

1. 不对运行中的库执行 `cp` 备份；
2. 不在 `--workers > 1` 下运行（采集锁与定时任务会重复）；
3. 不把 `schema_mode` 改成 `off` 来「绕过」启动失败；
4. 不在同一变更窗口同时改代码和数据语义；
5. **不在运行中的生产库上强制执行 `PRAGMA wal_checkpoint(TRUNCATE)`**（同理不执行 `VACUUM` / 整库替换），除非服务已停且已确认无任何写入者；
6. 不上传任何 `.db` 到网盘、仓库或聊天；
7. 不执行未经书面批准的批量删除/递归 ACL 命令、文件复制与源文件删除。

---

## 7. 状态

```
BINARY_RETAINED_PENDING_ISOLATION
SNAPSHOT_TRANSFER_NOT_AUTHORIZED
PRODUCTION_NO_GO
```

本轮仅输出方案，未部署、未迁移、未修改 ACL、未删除任何文件。
