# S6.11 发布责任与回滚

- 时间：2026-11-15
- 依据：`production-readonly-probe.md`、`database-write-boundary.md`
- **未执行任何回滚演练、未修改生产服务、未部署**

## 1. 责任表

| 角色 | 姓名 / 联系方式 | 状态 |
| --- | --- | --- |
| 上线负责人 | 未指定 | **PENDING** |
| 技术值班负责人 | 未指定 | **PENDING** |
| 告警接收人（P1/P2） | 未指定 | **PENDING** |
| 回滚决策人 | 未指定 | **PENDING** |
| 回滚执行人 | 未指定 | **PENDING** |
| 变更审批人 | 未指定 | **PENDING** |

> 任务书要求：未知人员不得自行填写。以上全部保持 `PENDING`。

## 2. 版本基线

| 项 | 值 | 状态 |
| --- | --- | --- |
| 生产当前版本 SHA | 无 `.git`；`main.py` blob 与仓库 50 个历史提交均不匹配 | **NOT_AVAILABLE** |
| 生产部署时间 | `main.py`/`config.py` mtime 2026-11-08 16:57，`backend` 22:13 | OBSERVED |
| 生产 `main.py` SHA256 | `04cb2afa6d7e29f0cec20ce9cd1ee7913d28c2f8274aeb3492e24f4248a1a220` | OBSERVED |
| 生产 app version | `0.3.0` | OBSERVED |
| 目标发布版本 | 分支 HEAD `48819deda0bf581511b36c6420b60072f4e76d6f` | 已知 |
| 目标发布包 | 尚未打包 | **NOT_AVAILABLE** |
| 目标发布包 SHA256 | 尚未生成 | **NOT_AVAILABLE** |
| 配置差异 | 见 `production-readonly-probe.md` §5 | OBSERVED |
| 数据库迁移差异 | `rps_daily` 缺 4 列，需显式迁移 | OBSERVED（阻断） |

## 3. 发布前检查清单（当前状态）

| 检查 | 状态 |
| --- | --- |
| 生产当前版本可回滚到上一版 | **NO**（无版本号、无上一版目录快照、无发布包） |
| 发布包 SHA256 | **NO** |
| 配置备份 | **NO**（未创建 `/opt/fupan-prev-*`） |
| 数据库完整备份 | **有条件 YES**：每日 23:10 备份，最近 11-13 `ok`，SHA256 已记录 |
| 异地备份 | **NO**（同盘） |
| 迁移脚本可用且在隔离库验证 | **YES**（`s7_migration_revalidate` PASS） |
| 迁移在生产执行 | **NO**（本轮禁止） |
| 健康检查端点可用于发布验收 | **部分**：`/api/health` 可用；`/api/ready` 不可用 |
| 回滚命令可执行性 | 需先创建目录快照；**未演练** |

## 4. 发布步骤（人工，未执行）

```bash
# 0) 记录当前状态
sudo -u fupan cp -p /opt/fupan/backend/app/main.py /tmp/main.py.before
sha256sum /opt/fupan/backend/app/main.py
sudo cp -a /opt/fupan /opt/fupan-prev-$(date +%Y%m%d-%H%M%S)

# 1) 备份数据库（应用同款在线备份；不要只 cp 主文件）
sudo -u fupan /opt/fupan/.venv/bin/python - <<'PY'
import sqlite3
src = sqlite3.connect('/opt/fupan/backend/data/fupan.db')
dst = sqlite3.connect('/opt/fupan/backend/data/fupan.db-bak-prerelease')
src.backup(dst); dst.close(); src.close()
PY

# 2) 数据库迁移（可空列、幂等、先备份）
sudo -u fupan /opt/fupan/.venv/bin/python scripts/migrate_rps_columns.py \
  --db /opt/fupan/backend/data/fupan.db --allow-main-db --apply \
  --backup /opt/fupan/backend/data/fupan.db-bak-prerelease

# 3) 部署新包并安装
sudo bash deploy/install.sh

# 4) 验收
curl -fsS http://127.0.0.1:8000/api/health
curl -fsS http://127.0.0.1:8000/api/live        # 目标版本新增（若已实施）
sudo -u fupan /opt/fupan/.venv/bin/python scripts/check_dist.py --base http://127.0.0.1:8000
```

⚠️ 迁移脚本默认拒绝主库，必须显式 `--allow-main-db`；执行前必须已有可用备份。

## 5. 回滚步骤（人工，未执行）

```bash
sudo systemctl stop fupan
sudo rsync -a --delete /opt/fupan-prev-<时间戳>/ /opt/fupan/
sudo systemctl restart fupan
curl -fsS http://127.0.0.1:8000/api/health
```

- **不自动恢复数据库**；
- 回滚兼容性：本次迁移只加可空列，旧版代码忽略新列 → **代码回滚兼容**；
- 若已写入不兼容数据，才按 `backup-restore-readiness.md` 人工恢复，并且先保留现场；
- 回滚失败：保持服务停止，保留日志与数据库文件，交回滚决策人（PENDING）。

## 6. 当前阻断项

1. 生产当前版本不可识别 → 无“上一版”可回滚；
2. 无发布包与 SHA256；
3. 无目录/配置快照；
4. 数据库缺列，未迁移；
5. 单库可写，无写入守卫；
6. 无异地备份；
7. 健康检查不可用于发布验收；
8. 责任人全部 `PENDING`。

## 7. 结论

```text
production_version_identified = NO
target_package_sha256 = NOT_AVAILABLE
config_snapshot = NO
db_migration_ready = YES (script) / NOT_EXECUTED (production)
rollback_dry_run = NOT_RUN (prohibited this round)
rollback_responsibility = PENDING
release_responsibility = BLOCKED
```
