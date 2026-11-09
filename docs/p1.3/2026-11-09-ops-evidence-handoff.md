# P1.3 生产运维证据采集交接单

> 日期：2026-11-09
> 发布候选：`fe2005f` / 工件 `fupan-fe2005f-r2.tar.gz`（SHA256 `85efc666…`）
> 本文件状态：**采集包已就绪，等待授权运维执行**。
> 本文档不是审计结果，不包含任何生产数据。

---

## 0. 为什么由运维执行

当前开发环境没有生产服务器地址、SSH 凭据或数据库路径，也不允许尝试获取。
本轮授权范围是「只读审计、备份与隔离演练准备」，因此：

- 研发：提供命令、校验返回内容、在副本上演练；**不接触生产**；
- 运维：执行下列四条命令，脱敏后回传前三份文本 + 备份元数据；
- 数据库文件（含真实业务/用户数据）走受控交付，**不上传仓库、不贴聊天**。

在收到证据前，生产门禁保持 **NO-GO**。

---

## 1. 四件事与产物

| # | 任务 | 命令（在服务器本机） | 产物 |
| 1 | 服务器资源 | `sudo bash deploy/prod_probe.sh > prod_probe.txt 2>&1` | `prod_probe.txt` |
| 2 | 生产 Schema | `python scripts/schema_inventory.py --db <副本> --json prod_schema.json` | `prod_schema.json` |
| 3 | 数据连续性 | `python scripts/prod_readonly_audit.py --db <副本> --start 2026-07-03 --end 2026-11-09 --json prod_audit.json` | `prod_audit.json` |
| 4 | 一致性备份 | 见 §3 | `backup_meta.json` + 备份文件 |

> 首次执行前可先 `bash -n deploy/prod_probe.sh` 自检语法。
> `ExecStart` 是 `python -m uvicorn app.main:app`，**不含数据库路径**，所以脚本的
> 「数据库文件与所在盘」一节会提示「未从 ExecStart 解析到 .db 路径」——
> 这是预期行为，请手工补两行：`ls -lh <库文件> <库文件>-wal` 与 `df -h <库目录>`。
> （该脚本已随候选冻结，本轮不再改动；自动解析可放到发布后的小版本。）

### 1.1 先做副本（任务 2/3 只允许在副本上跑）

```python
# /opt/fupan/audit/make_copy.py —— 用 SQLite 官方备份 API，不要直接 cp 主文件
import sqlite3
src = sqlite3.connect("file:/srv/fupan/backend/data/fupan.db?mode=ro", uri=True)
dst = sqlite3.connect("/srv/fupan/audit/prod_copy.db")
src.backup(dst)
print(dst.execute("PRAGMA quick_check").fetchone())
dst.close(); src.close()
```

副本只留在 `/srv/fupan/audit/` 受控目录；回传 JSON，不回传 `.db`。

---

## 2. 脱敏清单（回传前逐项检查）

必须删除或替换：

- `systemctl cat` 输出里的 `Environment=` 行（token、webhook、SMTP 密码等）；
- 数据库绝对路径中的主机名 / 用户名；
- 内网域名、IP、备案号、邮箱、手机号；
- `collect_log.message` 中可能出现的第三方账号；
- `journalctl` 里的请求 URL 参数。

保留：

- 内存 / CPU / 磁盘 / 进程 RSS 数字；
- `MemoryMax`、`TasksCurrent`、Worker 数、nginx 超时值；
- 表名、列名、行数、日期、状态分布。

---

## 3. 一致性备份

```bash
systemctl stop fupan                      # 或确认调度已停止（由运维按维护流程执行）
python /srv/fupan/audit/make_backup.py    # 备份到 /backup/fupan-YYYYMMDD-HHMM.db
sqlite3 /backup/fupan-YYYYMMDD-HHMM.db "PRAGMA quick_check;"
sha256sum /backup/fupan-YYYYMMDD-HHMM.db
```

要求：

- 明确书面授权与执行人；
- `quick_check = ok`；
- 独立保存一份到受控异地（不与在线库同盘）；
- 备份文件本身**不入 Git、不上传公开存储**；
- 通过受控通道交付给演练环境，并登记接收人。

备份元数据模板（回传这一份即可）：

```json
{
  "file": "fupan-20261110-1030.db",
  "size_bytes": 0,
  "sha256": "",
  "created_at": "2026-11-10T10:30:00+08:00",
  "quick_check": "ok",
  "source_path_masked": "/srv/fupan/backend/data/fupan.db",
  "service_state": "stopped",
  "scheduler_state": "stopped",
  "operator": "<姓名/工号>",
  "stored_at": "<受控目录/介质>",
  "offsite_copy": true
}
```

---

## 4. 回传文件的验收标准

### 4.1 `prod_probe.txt`

必须包含：`free -m`、`nproc`、`MemoryCurrent/MemoryMax/TasksCurrent`、
`ExecStart`（可见 Worker 数）、`ps --sort=-rss` 前 25 行、`df -h`、
库文件大小与所在盘、nginx 三个超时、采集期 RSS 峰值。

### 4.2 `prod_schema.json`

必须包含表 / 列（类型、非空、默认值、主键）/ 索引（含唯一性）/ 触发器 / 视图。
研发将执行：

```
python scripts/schema_inventory.py --db <发布基线副本> --compare <生产副本> --diff-json diff.json
```

### 4.3 `prod_audit.json`

必须包含：四张主表的 `rows / min_date / max_date / trade_days`；
`--start/--end` 的逐日矩阵；`rps` 新字段与 `orphan_dates`；`manifest` 状态分布；
`collect_log.last_ok_by_task` 与最近 10 条非 ok。

### 4.4 判定要点（研发收到后）

- 生产最新完整交易日是哪天（不得把当天未完成的盘后数据算缺口）；
- `stock_daily` / `rps_daily` / `sector_daily` / `sector_member` 的首末日期与逐日缺口；
- 生产是否存在本机研究库没有的表 / 列 / 数据（尤其 `analysis_snapshot`、
  `user_risk_pref`、`trade_plan` 13 列、`sector_member_backfill`）；
- RPS 孤儿日期（有 RPS 无当日行情）清单；
- 生产 Schema 与候选的差集，区分「待新增」与「冲突/不兼容」。

---

## 5. 收到证据后的执行顺序（不再改代码）

1. 校验 `prod_probe.txt` 完整性，确认单 Worker 与内存余量；
2. 在**副本**上跑 `schema_inventory --compare`，列出待迁移对象；
3. 按生产主键重新计算 RPS / 成分股 / manifest 差集（不套用本机 1,262,706）；
4. 显式迁移（`migrate_db.py --apply`）→ 幂等复跑；
5. 增量导入 → 冲突逐条裁决 → 用户表行数前后一致；
6. 安装 24 个触发器并验证 8 域 ready；
7. verify 模式启动候选应用（首次 `CATCHUP_ON_START=false`）；
8. 旧接口 + 研究接口 + 鉴权 + 浏览器 E2E；
9. 资源与并发检查（单 Worker、并发 1、RSS 峰值）；
10. 回滚演练（备份恢复 + 回滚后启动必须失败关闭）；
11. 出具逐项证据；
12. 项目负责人签字后，另案申请生产发布授权。

流程细节见 `docs/p1.1/2026-11-09-p1.1-backup-drill.md`。

---

## 6. 当前门禁

| 门禁 | 状态 |
| --- | --- |
| 发布工件冻结与可复现 | ✅ `fupan-fe2005f-r2.tar.gz` / 232 文件 / `85efc666…` |
| 服务器资源 | ❌ 等待 `prod_probe.txt` |
| 生产 Schema | ❌ 等待 `prod_schema.json` |
| 数据连续性 | ❌ 等待 `prod_audit.json` |
| 一致性备份 | ❌ 等待 `backup_meta.json` 与受控副本 |
| 生产迁移/部署 | ❌ 未授权 |

本文件不改变 NO-GO 结论。
