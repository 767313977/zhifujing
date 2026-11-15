# S6.12 生产升级执行报告

- 时间：2026-11-15 18:18–18:23（Asia/Shanghai）
- 执行人：自动化（用户已明确授权本次生产升级）
- 结论：**升级成功，服务已恢复运行；登录后功能验收由用户完成。**

## 1. 版本与发布包

| 项 | 值 |
| --- | --- |
| 生产 Git SHA | **无 `.git`**（`/opt/fupan` 不含仓库） |
| 升级前代码指纹 | `main.py` `04cb2afa6d7e29f0cec20ce9cd1ee7913d28c2f8274aeb3492e24f4248a1a220` |
| 目标源码提交 | `a8b13a6`（S6.12 代码与测试修正） |
| 发布包 | `fupan-2026-11-15.tar.gz` |
| 发布包 SHA256（本地/远端一致） | `447af53308b081001c99779e911cd0e3e75da13dd3f362b0d0f9b12a5d755f55` |
| 升级后 `main.py` SHA256 | `792dfcb248dc0746c0ec7b609ecce99bcc72e389c9ade70567e4ac6bbac348af`（与包内一致） |
| `runtime_context.py` | 已部署 |
| 前端产物自检 | 通过：57 个引用全部可访问 |

## 2. 数据库备份（升级前）

| 项 | 值 |
| --- | --- |
| 文件 | `/opt/fupan/backend/data/fupan.db-prerps-20261115-174841` |
| 大小 | 1,834,639,360 字节 |
| SHA256 | `c6645d8ccd02f84acfc1c620ef9536cf7ff8469fe3caa02be23a66f2979d5a90` |
| integrity_check | **ok** |
| `stock_daily` | 2,627,509 行，最新 2026-11-13 |
| `limit_pool` | 31,253 行 |
| `app_user` | 5 |
| `auction_snapshot` | 139,485 行 |
| 历史备份 | 未删除、未覆盖（11-09 ~ 11-13 五份仍在） |

## 3. Schema 迁移结果

迁移前 → 后：

```text
迁移前: trade_date, code, ret120, ret250, rps120, rps250, ret50, rps50
迁移后: 上述 8 列 + ret20, ret60, rps20, rps60
```

| 检查 | 结果 |
| --- | --- |
| 首次迁移 | 4 列全部添加，exit 0 |
| 第二次迁移（幂等） | 全部“已存在，跳过” |
| rps_daily 行数 | 178,873 → 178,873 |
| 旧列 SUM 摘要 | 6 项完全一致 |
| 前 500 行摘要 SHA256 | `85075844…` 前后一致 |
| 迁移后 integrity_check | **ok** |
| 服务启动 `schema=verify` | 通过，未执行任何 DDL |

说明：生产 `/opt/fupan/scripts` 是旧包，**没有** `migrate_rps_columns.py`；迁移脚本从目标包取出后经 `/tmp` 执行（SHA256 `ab010539…`），未改动生产脚本目录。

## 4. 部署过程与停机

| 阶段 | 时间 |
| --- | --- |
| 停止服务 | 18:18:38 |
| 迁移完成 + integrity | 18:20:56 |
| 新服务启动 | 18:21:35 |
| 验证完成 | 18:23 左右 |
| 停机时长 | 约 **2 分 57 秒** |

停机影响：当时无采集任务在跑（下一次 20:00 告警、23:10 备份；竞价在下一交易日 09:15），无数据丢失。

## 5. 服务状态

```text
systemctl is-active fupan = active
PID = 3476546
启动时间 = 2026-11-15 18:21:35 CST
NRestarts = 0
http://127.0.0.1:8000/api/health = 200
https://zhifujing.top/ = 200
https://zhifujing.top/api/health = 200
```

- 启动日志：`Schema 检查通过（verify，未执行任何 DDL）`、`数据库就绪`；
- 无 ERROR / Traceback。

## 6. 调度状态

- `SCHEDULER_ENABLED=true`，调度器已启动；
- 日志显示任务注册：15:05 主采集、17:30/19:30/22:35 晚间、22:00/22:30/08:30 成分股、**09:15–09:26 + 09:31 集合竞价**、20:00 告警、14:50 B1、23:10 备份；
- **竞价任务按用户“不关闭真实采集”的要求保留**：新增 systemd drop-in
  `/etc/systemd/system/fupan.service.d/auction.conf`（`AUCTION_COLLECT_ENABLED=true`），
  进程环境实测为 true；
- 未修改其它调度时刻。

> 目标包 `install.sh` 的默认值是 `AUCTION_COLLECT_ENABLED=false`；本次用 drop-in 覆盖为 true，
> 保持升级前行为。若希望采用新默认（关闭竞价），删除该 drop-in 并重启即可。

## 7. 核心 API 验收

| 检查 | 结果 |
| --- | --- |
| `/api/health` | 200 |
| 公网 `/`、`/login` | 200（标题“致富经”） |
| 未登录访问 `/api/market/overview` | 401（鉴权边界正常） |
| `/api/ready` | 503（该端点仍是 fail-closed 安全门禁，监控请用 `/api/health`） |
| 数据库 `quick_check` | ok |
| `rps_daily` 4 新列 | 存在 |
| `stock_daily` / `limit_pool` | 行数与备份一致 |
| 登录后首页/个股/板块 | **由用户本人验收**（按要求不索取密码） |

## 8. 回滚状态

- **未触发回滚**；
- 升级前完整副本：`/opt/fupan-prev-20261115-1800`（345 文件，7.9 MB，`main.py` 与升级前一致）；
- 本机另存：`dist-deploy/rollback-prod-20261115.tar.gz`（SHA256 `6a377886…`）；
- 回滚命令见 `03-backup-rollback.md`；
- 回滚时不删除新增的 4 个可空列，不自动恢复数据库。

## 9. 未解决问题

1. 生产无 `.git`，版本仍只能以“发布包 SHA256 + `main.py` 指纹”标识；
2. 登录后功能（首页/个股/板块/自选）待用户本人验收；
3. `/api/ready` 仍为 503（不影响服务，监控用 `/api/health`）；
4. 异地备份、监控告警、OS Egress、24h Soak 按既定计划延期，未标 PASS；
5. 手动同步已停用（返回 409），新票首次打开可能暂无本地 K 线，等每日采集。

## 10. 升级后状态判定

```text
backup_before_upgrade = PASS (integrity ok, SHA256 recorded)
rps_migration = PASS (idempotent, data unchanged, integrity ok)
package_upload_sha256 = PASS
deployment = PASS (check_dist 57/57)
service_restart = PASS (active, health 200)
database_after_upgrade = PASS (quick_check ok, counts unchanged)
scheduler = PASS (auction preserved)
public_site = PASS
post_login_acceptance = PENDING (user)
rollback = NOT_TRIGGERED
```
