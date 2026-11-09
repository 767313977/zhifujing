# P1.3 生产运维证据采集交接单（r2）

> 日期：2026-11-09（r2，按 P1.3 最终修订指令更正执行顺序、审计目标库确认、时间边界与权限要求）
> 发布候选：`fe2005f` / 工件 `fupan-fe2005f-r2.tar.gz`（SHA256 `85efc666…`，保持不变）
> 当前开发 HEAD：`da97af2`
> 本文件状态：**采集包已就绪，等待授权运维执行**。
> 本文档不是审计结果，不包含任何生产数据。本次仅修订交接文档，冻结的应用源码、
> 部署脚本与发布工件均未改动。

---

## 0. 为什么由运维执行

当前开发环境没有生产服务器地址、SSH 凭据或数据库路径，也不允许尝试获取。
本轮授权范围是「只读审计、备份与隔离演练准备」，因此：

- 研发：提供命令、校验返回内容、在副本上演练；**不接触生产**；
- 运维：按 §1 的顺序执行，脱敏后回传四份文件；
- 数据库文件（含真实业务/用户数据）走受控交付，**不上传仓库、不贴聊天**。

在收到证据前，生产门禁保持 **NO-GO**。

---

## 1. 执行顺序与产物（**严格按序**，不得调换）

| 顺序 | 任务 | 命令 / 方式 | 产物 |
| 1 | 服务器资源（只读，不接触数据库） | `sudo bash deploy/prod_probe.sh > prod_probe.txt 2>&1` | `prod_probe.txt` |
| 2 | 创建生产 SQLite **一致性备份**，完整性检查，记录时间/大小/SHA256 | 见 §3 | `backup_meta.json` + 备份文件（受控保存） |
| 3 | 用**任务 2 的同一份**备份副本生成 Schema 清单 | 见 §4 | `prod_schema.json` |
| 4 | 用**任务 2 的同一份**备份副本生成数据连续性审计 | 见 §4、§5 | `prod_audit.json` |

**硬性规则：**

- 任务 3、4 必须运行在任务 2 产生的**同一个一致性快照**上。
  **禁止**在两个不同时间点的数据库副本上分别生成 Schema 与数据报告，
  却将其标记为同一次一致性快照。
- 如果审计使用**事先存在的备份**，必须先确认其来源、创建时间、完整性
  （`quick_check` 与 SHA256）及身份与本次审计要求相符，并在回传说明中注明；
  无法确认的一律重新执行任务 2。
- 最后汇总回传：`prod_probe.txt`、`prod_schema.json`、`prod_audit.json`、
  `backup_meta.json`（均先按 §7 脱敏）。

> 首次执行前可先 `bash -n deploy/prod_probe.sh` 自检语法。

---

## 2. 数据库路径与文件权限

- `prod_probe.sh` **保持冻结版本，本轮不改**。
- 已知限制：`ExecStart` 是 `python -m uvicorn app.main:app`，**不含数据库路径**，
  脚本「数据库文件与所在盘」一节会提示「未从 ExecStart 解析到 .db 路径」——
  这是预期行为。
- 由授权运维根据实际服务配置（`systemctl cat fupan`、应用配置）**确认**
  数据库文件位置，再手工补两行：`ls -lh <库文件> <库文件>-wal` 与
  `df -h <库目录>`。**不可猜测数据库路径。**
- 执行探针与审计前：先 `umask 077`，输出一律写入受控目录
  （如 `/srv/fupan/audit/`），防止原始审计信息被同机其他用户读取。

---

## 3. 一致性备份（任务 2）

```bash
systemctl stop fupan                      # 或确认调度已停止（由运维按维护流程执行）
python /srv/fupan/audit/make_backup.py    # 用 SQLite 备份 API 备到 /backup/fupan-YYYYMMDD-HHMM.db
sqlite3 /backup/fupan-YYYYMMDD-HHMM.db "PRAGMA quick_check;"
sha256sum /backup/fupan-YYYYMMDD-HHMM.db
```

要求：

- 明确书面授权与执行人；
- `quick_check = ok`；
- 记录备份**创建时间、大小、SHA256**（填入下面的元数据模板）；
- 独立保存一份到受控异地（不与在线库同盘）；
- 备份文件本身**不入 Git、不上传公开存储**；
- 通过受控通道交付给演练环境，并登记接收人。

**审计工作副本：** 备份文件是已完成的离线一致快照，可直接 `cp` 一份到
`/srv/fupan/audit/prod_copy.db` 作为审计工作副本（`cp` 后 SHA256 应与备份相同，
请核验）。任务 3、4 只对这个工作副本操作；备份原件保持不动。

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

## 4. 审计目标库确认（任务 3、4 执行前必做）

两个脚本的参数契约（**以服务器上实际 `--help` 输出为准，不得猜测参数**）：

```
python scripts/schema_inventory.py --db <库> [--compare <库>] [--json <文件>] [--diff-json <文件>]
    --db 帮助原文：「要清点的库（只读打开）」
python scripts/prod_readonly_audit.py --db <副本> [--start <日>] [--end <日>] [--json <文件>]
    --db 帮助原文：「备份副本路径（以 mode=ro 打开）」
```

执行命令（在受控目录）：

```bash
python scripts/schema_inventory.py --db /srv/fupan/audit/prod_copy.db --json prod_schema.json
python scripts/prod_readonly_audit.py --db /srv/fupan/audit/prod_copy.db \
    --start 2026-07-03 --end 2026-11-09 --json prod_audit.json
```

执行前记录、随报告一并回传：

- **已验证的数据库副本绝对路径**（必须显式指向 §3 的审计工作副本；
  **禁止**默认使用开发机的 `backend/data/fupan.db`）；
- **副本 SHA256**（必须等于 `backup_meta.json` 中的值）；
- **工具版本**：两个脚本文件的 SHA256 + 冻结源码提交 `fe2005f`
  （证明服务器上的脚本与候选一致）；
- **审计范围**：`--start/--end` 与覆盖表；
- **只读确认**：脚本以 `mode=ro` 打开；运行前后副本 SHA256 不变，
  且不产生 `-wal`/`-shm` 边车文件。审计期间禁止执行任何迁移或写入。

---

## 5. 数据审计的时间边界（任务 4）

- `--start 2026-07-03 --end 2026-11-09` 保留不变。
- 回传说明中注明**审计执行时刻**，并区分四类日期，逐类说明：
  1. **已完成**全部盘后采集的最新交易日；
  2. **正在采集**的交易日（部分完成）；
  3. **尚未到达调度时间**的采集任务（例如当日 22:00、22:30、22:35 等任务
     在执行时刻之前到点的才算，未到点的**不得**判定为失败或数据缺失）；
  4. **真正缺失或失败**的数据。
- 不得把部分完成的当天数据当作完整数据；也不得把尚未到期的任务计为异常。

---

## 6. （保留）探针输出要求

`prod_probe.txt` 必须包含：`free -m`、`nproc`、
`MemoryCurrent/MemoryMax/TasksCurrent`、`ExecStart`（可见 Worker 数）、
`ps --sort=-rss` 前 25 行、`df -h`、库文件大小与所在盘（按 §2 手工补）、
nginx 三个超时、采集期 RSS 峰值。

---

## 7. 脱敏清单（回传前逐项检查）

必须删除或替换：

- `systemctl cat` 输出里的 `Environment=` 行（token、webhook、SMTP 密码等）；
- 数据库绝对路径中的主机名 / 用户名；
- 内网域名、IP、备案号、邮箱、手机号；
- `collect_log.message` 中可能出现的第三方账号；
- `journalctl` 里的请求 URL 参数。

保留：

- 内存 / CPU / 磁盘 / 进程 RSS 数字；
- `MemoryMax`、`TasksCurrent`、Worker 数、nginx 超时值；
- 表名、列名、行数、日期、状态分布；
- §4 要求的路径验证、SHA256、工具版本与只读确认记录（路径可脱敏，哈希保留）。

---

## 8. 回传文件的验收标准

### 8.1 `prod_probe.txt`

见 §6。

### 8.2 `prod_schema.json`

必须包含表 / 列（类型、非空、默认值、主键）/ 索引（含唯一性）/ 触发器 / 视图；
并附 §4 的副本路径、副本 SHA256、脚本 SHA256、只读确认记录。
研发将执行：

```
python scripts/schema_inventory.py --db <发布基线副本> --compare <生产副本> --diff-json diff.json
```

### 8.3 `prod_audit.json`

必须包含：四张主表的 `rows / min_date / max_date / trade_days`；
`--start/--end` 的逐日矩阵；`rps` 新字段与 `orphan_dates`；`manifest` 状态分布；
`collect_log.last_ok_by_task` 与最近 10 条非 ok；
并附 §5 的四类日期划分说明与审计执行时刻。

### 8.4 一致性核验（研发收到后先做）

- `prod_schema.json` 与 `prod_audit.json` 记录的副本 SHA256 **必须相同**，
  且等于 `backup_meta.json` 的 SHA256 —— 三者不一致直接退回；
- 生产最新完整交易日是哪天（不得把当天未完成的盘后数据算缺口）；
- `stock_daily` / `rps_daily` / `sector_daily` / `sector_member` 的首末日期与逐日缺口；
- 生产是否存在本机研究库没有的表 / 列 / 数据（尤其 `analysis_snapshot`、
  `user_risk_pref`、`trade_plan` 13 列、`sector_member_backfill`）；
- RPS 孤儿日期（有 RPS 无当日行情）清单；
- 生产 Schema 与候选的差集，区分「待新增」与「冲突/不兼容」。

---

## 9. 交付物与最终门禁

运维交付：

1. `prod_probe.txt`（脱敏）
2. `prod_schema.json`（脱敏）
3. `prod_audit.json`（脱敏）
4. `backup_meta.json`
5. 经授权、受控保存并受控交付的**一致性数据库备份副本**

收到上述材料后，才允许在**隔离环境**执行既定的 12 步迁移、导入、验证与
回滚演练（§10）。

本阶段仍然**禁止**：push、合并、生产迁移、生产历史导入、生产部署、新增补采。
不再启动新的软件开发阶段。

---

## 10. 收到证据后的执行顺序（不再改代码）

1. 校验 `prod_probe.txt` 完整性，确认单 Worker 与内存余量；
2. 先做 §8.4 的三份 SHA256 一致性核验；
3. 在**副本**上跑 `schema_inventory --compare`，列出待迁移对象；
4. 按生产主键重新计算 RPS / 成分股 / manifest 差集（不套用本机 1,262,706）；
5. 显式迁移（`migrate_db.py --apply`）→ 幂等复跑；
6. 增量导入 → 冲突逐条裁决 → 用户表行数前后一致；
7. 安装 24 个触发器并验证 8 域 ready；
8. verify 模式启动候选应用（首次 `CATCHUP_ON_START=false`）；
9. 旧接口 + 研究接口 + 鉴权 + 浏览器 E2E；
10. 资源与并发检查（单 Worker、并发 1、RSS 峰值）；
11. 回滚演练（备份恢复 + 回滚后启动必须失败关闭）；
12. 出具逐项证据，项目负责人签字后，另案申请生产发布授权。

流程细节见 `docs/p1.1/2026-11-09-p1.1-backup-drill.md`。

---

## 11. 当前门禁

| 门禁 | 状态 |
| --- | --- |
| 发布工件冻结与可复现 | ✅ `fupan-fe2005f-r2.tar.gz` / 232 文件 / `85efc666…` |
| 服务器资源 | ❌ 等待 `prod_probe.txt` |
| 一致性备份 | ❌ 等待 `backup_meta.json` 与受控副本 |
| 生产 Schema | ❌ 等待 `prod_schema.json`（须与备份同一快照） |
| 数据连续性 | ❌ 等待 `prod_audit.json`（须与备份同一快照） |
| 生产迁移/部署 | ❌ 未授权 |

本文件不改变 NO-GO 结论。
