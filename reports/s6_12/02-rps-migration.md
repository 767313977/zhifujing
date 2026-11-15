# S6.12 P0-2：RPS Schema 迁移方案与演练

- 时间：2026-11-15
- 生产缺失列：`rps_daily.ret20` / `ret60` / `rps20` / `rps60`
- 生产迁移：**未执行**（等待用户单独授权）
- 演练证据：`03-rps-migration-evidence.json` / `.md`

## 1. 迁移脚本审查（`scripts/migrate_rps_columns.py`）

| 要求 | 结论 |
| --- | --- |
| 幂等 | **是**：已存在的列打印“已存在，跳过” |
| 检测已有字段 | `PRAGMA table_info(rps_daily)` |
| 不重复添加 | 是 |
| 不覆盖已有数据 | `ALTER TABLE ... ADD COLUMN FLOAT`（可空，不重写行） |
| 不改变其他表 | 只操作 `rps_daily` |
| 事务行为 | SQLite DDL 隐式提交；4 条 ADD COLUMN 逐条提交，失败会停在已加列的中间态 |
| 失败恢复 | 重跑即可补齐（幂等）；可用 `--rollback` 删除 4 列 |
| WAL 兼容 | 演练使用 WAL 副本通过 |
| 记录前后 schema | 脚本输出“迁移前列 / 迁移后列” |
| 记录完整性 | 演练中前后 `PRAGMA integrity_check` 均为 ok |
| 主库保护 | 默认拒绝 `backend/data/fupan.db`，需显式 `--allow-main-db` |

## 2. 隔离演练结果（生产结构副本）

| 项 | 值 |
| --- | --- |
| 源库 | `backend/data/fupan.db`（旧 schema，`rps_daily` 8 列 / 10,206 行） |
| 方式 | 源库 `mode=ro` → 在线备份到临时副本；演练后删除 |
| 备份 SHA256 | `43d1e4ba965940430bf4579e47314533042247e56823122e91265a435051d170` |
| 迁移前 | `trade_date, code, ret120, ret250, rps120, rps250, ret50, rps50` |
| 迁移后 | 上述 8 列 + `ret20, ret60, rps20, rps60` |

| 检查 | 结果 |
| --- | --- |
| 备份可读、integrity | PASS |
| 备份旧列数据与源一致 | PASS |
| 首次迁移 exit 0 | PASS |
| 4 个新列存在、旧列保留 | PASS |
| 第二次迁移（幂等） | PASS（全部“已存在，跳过”） |
| 新列加入后旧列数据摘要不变 | PASS（10,206 行，SHA256 一致） |
| 迁移后 integrity | PASS |
| 模拟中断（删 `rps20`）后重跑 | PASS（自动补回） |
| 恢复后数据摘要一致 | PASS |
| 旧版本 SQL（ret50/120/250、rps50/120/250）可查 | PASS |
| 新版本 `schema_report(include_user_models=False)` | **ok=true**，无缺表/缺列/缺索引 |

> 本地 `fupan.db` 的用户表比当前版本旧（缺 review_note/trade_* 新列），所以新版本
> 校验只针对研究表；生产实测用户表**已包含**这些列（S6.11 探测），不受影响。

## 3. 生产迁移步骤（待授权，不自动执行）

```bash
# 1) 先做一次在线一致性备份（不要只 cp 主文件）
sudo -u fupan /opt/fupan/.venv/bin/python - <<'PY'
import sqlite3
src = sqlite3.connect('/opt/fupan/backend/data/fupan.db')
dst = sqlite3.connect('/opt/fupan/backend/data/fupan.db-bak-prerps')
src.backup(dst); dst.close(); src.close()
PY
sha256sum /opt/fupan/backend/data/fupan.db-bak-prerps
sqlite3 /opt/fupan/backend/data/fupan.db-bak-prerps "PRAGMA integrity_check;"

# 2) 停止服务（避免迁移期间写入）
sudo systemctl stop fupan

# 3) 执行迁移（显式允许主库）
sudo -u fupan /opt/fupan/.venv/bin/python /opt/fupan/scripts/migrate_rps_columns.py \
  --db /opt/fupan/backend/data/fupan.db --allow-main-db --apply

# 4) 复核
sqlite3 /opt/fupan/backend/data/fupan.db "PRAGMA table_info(rps_daily); PRAGMA integrity_check;"

# 5) 部署新版本并启动
sudo bash /opt/fupan/deploy/install.sh
sudo systemctl status fupan --no-pager
curl -fsS http://127.0.0.1:8000/api/health
```

## 4. 发布顺序与回滚

推荐顺序：**先迁移，后部署**。

- 旧版本忽略新增可空列，迁移后旧服务仍可运行；
- 新版本在未迁移时会 `schema_mode=verify` 启动失败，所以顺序不能反；
- 若新版本启动失败：`systemctl stop fupan`，切回旧目录（见 `03-backup-rollback.md`），
  **保留新增列**（不删除可能已被使用的数据列）；
- 只有在确认新列完全无用且需要彻底回退时，才使用 `migrate_rps_columns.py --rollback`。

## 5. 风险

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| 迁移中断 | 可能只加了部分列 | 重跑脚本即可（幂等）；已演练 |
| 长事务/锁 | ADD COLUMN 很快（表 1 万级），生产 `rps_daily` 行数需确认 | 停服务执行，窗口内完成 |
| 磁盘 | 迁移不复制整表；SQLite 可能临时写 journal | 生产余量 36G，足够 |
| 旧版本兼容 | 旧代码显式选择旧列 | 已用旧列 SQL 验证；建议迁移后先观察再部署 |
