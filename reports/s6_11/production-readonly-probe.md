# S6.11 生产只读探测报告

- 时间：2026-11-15 16:43–16:56（Asia/Shanghai）
- 主机：`<生产服务器IP>`（IP 已脱敏；`zhifujing.top`，腾讯云轻量，Ubuntu 24.04.5）
- 方式：`ubuntu` + `~/.ssh/fupan_deploy`，`sudo -n` 可用；**全流程只读**
- 证据：`evidence/prod-probe-raw.txt`（综合探测，exit 0）、
  `evidence/prod-probe-with-rss-raw.txt`（`deploy/prod_probe.sh` 原样执行，含 120s RSS 采样）
- 机器可读：`production-readonly-probe.json`
- 状态口径：`OBSERVED` = 实测；`NOT_AVAILABLE` = 不存在/拿不到；`NOT_VERIFIED` = 只读约束下无法证明；
  `FAILED` = 命令失败。**本轮没有 FAILED 项**。

## 0. 命令与退出码

| 轮次 | 命令 | 时间 | 退出码 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | `deploy/prod_probe.sh` 经 stdin 执行 `sudo bash -s` | 16:43 | 127 | 输出完整；127 来自 Windows 管道末尾 `\r`，非探测失败 |
| 2 | 补充探测（环境/数据库/证书/调度/防火墙） | 16:50 | 127 | 同上（CRLF 管道），输出完整 |
| 3 | 复查（表/列/竞价/证书/worker） | 16:52 | 127 | 同上，输出完整 |
| 4 | 综合探测（base64 传输，`sudo bash`） | 16:55 | **0** | 正式证据，无 CRLF 干扰 |

## 1. OS / 内核 / CPU / 内存 / 磁盘 — OBSERVED

| 项 | 实测 |
| --- | --- |
| OS | Ubuntu 24.04.5 LTS |
| 内核 | 6.8.0-147-generic |
| 运行时长 | 3 周 1 天 17 小时 |
| CPU | 2 vCPU |
| 内存 | 3723 MB（可用 2909 MB），Swap 1987 MB（用 45 MB） |
| 根盘 | 59G，已用 21G，**可用 36G（37%）** |
| inode | 5% |
| DB 目录 | `backend/data` 合计 **11G** |

## 2. 当前版本 — NOT_AVAILABLE（无法映射到提交）

| 项 | 实测 |
| --- | --- |
| `/opt/fupan/.git` | **不存在** |
| `main.py` mtime | 2026-11-08 16:57 |
| `main.py` SHA256 | `04cb2afa6d7e29f0cec20ce9cd1ee7913d28c2f8274aeb3492e24f4248a1a220` |
| `app.version` | `0.3.0` |
| 与仓库提交比对 | 对 50 个改过 `main.py` 的提交做 blob SHA256 比对 → **NO_MATCH** |
| 结论 | 生产是 **2026-11-08 的部署包**，无法对应任何仓库提交；**当前生产 SHA = NOT_AVAILABLE** |

## 3. 部署目录 — OBSERVED

- `/opt/fupan`（`fupan:fupan`，755），`backend` mtime **2026-11-08 22:13**；
- `frontend/dist` mtime 2026-09-18（此后前端未重新部署）；
- `backend/data` 700，`fupan.db` 600。

## 4. 服务进程与运行时间 — OBSERVED

| 项 | 实测 |
| --- | --- |
| 单元 | `fupan.service`（`Restart=always`） |
| PID | 3300601，启动于 **2026-11-15 06:08:42 CST** |
| 命令 | `uvicorn app.main:app --host 127.0.0.1 --port 8000` |
| Worker | **1 个进程 / 9 线程**（未使用 `--workers`，符合单调度器要求） |
| RSS | 301.4 MB；cgroup 336 MB |
| 采集期 RSS 峰值 | 294.3 MB（120 秒采样） |
| nginx 最近日志 | 306 请求、**5xx = 0**、4xx = 114、平均 0.118s、最大 19.1s |

## 5. 环境变量与配置状态 — OBSERVED

systemd（非敏感值）：

```text
PYTHONUNBUFFERED=1
TZ=Asia/Shanghai
SCHEDULER_ENABLED=true
IFIND_CA_FILE=/etc/ssl/certs/ca-certificates.crt
ENABLE_DEV_CORS=false
WARMUP_PATTERN_CACHE=true
```

`.env` **仅列出键名**：`BACKUP_KEEP`、`FEISHU_TARGET`、`FEISHU_WEBHOOK_URL`、`HOST`、
`IFIND_AUTH_TOKEN`、`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`、`SCHEDULER_ENABLED`。
值未读取、未回传。

⚠️ 关键差异：

| 配置 | 生产 | 目标版本（S6.10 代码） |
| --- | --- | --- |
| `DB_SPLIT_ENABLED` | **不存在** → 单库 | 支持拆分 |
| `RESEARCH_DB_READONLY` | 不存在 | 可开 |
| `AUCTION_COLLECT_ENABLED` | **不存在** | 默认 false |
| `FUPAN_ENV` | 未设置（development） | 建议 production |
| `WARMUP_PATTERN_CACHE` | true | 生产会触发 readiness 配置错误 |

## 6. 数据库路径 / 数量 / 大小 / 权限 — OBSERVED

| 项 | 实测 |
| --- | --- |
| 数量 | **1 个库** |
| 路径 | `/opt/fupan/backend/data/fupan.db` |
| 主库 | 1,834,102,784 字节（1.83 GB） |
| WAL | 49,571,872 字节（49.5 MB，mtime 11-15 03:30） |
| SHM | 32,768 字节 |
| 权限 | 文件 600 `fupan:fupan`；目录 700 |
| journal_mode | **WAL**（`mode=ro` 读取；`immutable=1` 会显示 main header 的 delete，属读取方式差异） |

## 7. Schema 与迁移版本 — OBSERVED（**发现问题**）

| 项 | 实测 |
| --- | --- |
| 表数量 | **49** |
| `PRAGMA user_version` | 0（未使用版本号，迁移靠结构判断） |
| 目标版本要求表 | 47 张 → **全部存在** |
| 生产多出的表 | `etf_share_orphan_backup_20261016`（历史孤儿备份）、`sector_fund_flow`（旧表） |
| **缺失列** | **`rps_daily` 缺 `ret20` / `ret60` / `rps20` / `rps60`** |
| 影响 | 目标版本以 `schema_mode=verify` 启动会**失败**；需先执行 `scripts/migrate_rps_columns.py --apply`（可空列、幂等、可回滚） |

## 8. Research DB / User DB 读写模式 — OBSERVED（**单库可写**）

| 证据 | 结论 |
| --- | --- |
| 运行时无 `DB_SPLIT_ENABLED` | 拆分未开启 |
| 生产 `config.py` 无 `db_split_enabled` 字段 | 该版本根本没有拆分能力 |
| 只有一个 `fupan.db`，无 `user.db` | 研究数据与用户数据同库 |
| 目录 700、文件 600 | 操作系统层保护正常，但**进程内无库级只读** |

## 9. 数据库完整性 — OBSERVED

`PRAGMA integrity_check` = **ok**（`mode=ro`，未 checkpoint、未写库）。

## 10. 最近备份 — OBSERVED

| 项 | 实测 |
| --- | --- |
| 最近一份 | `fupan.db-bak-20261113-231000-001069`，2026-11-13 23:10 |
| 大小 | 1,834,606,592 字节 |
| SHA256 | `c96b29b27b93912b455d9bda1b1e7c0d9a4cc4da8dd3b2564f733f20d2b6efd3` |
| 保留 | 5 份（11-09 ~ 11-13，约 8.9 GB） |
| 计划 | `mon-fri 23:10`；11-14/15 为周末，未跑属正常 |
| 最近 5 次状态 | 全部 `ok` |
| **异地** | **未配置**（日志原文：“未配置异地同步（备份与主库同盘）”） |

## 11. HTTPS / TLS / 反向代理 — OBSERVED

| 项 | 实测 |
| --- | --- |
| nginx | active；`server_name zhifujing.top www.zhifujing.top` |
| TLS | TLSv1.2 / TLSv1.3 |
| 证书 | Let's Encrypt（CN=YE1），有效期 **2026-09-28 ~ 2026-12-27** |
| 续期 | `certbot.timer` active，`/etc/letsencrypt/renewal/zhifujing.top.conf` 存在 |
| HSTS | `max-age=31536000; includeSubDomains` |
| 反代 | `proxy_pass http://127.0.0.1:8000`；`auth_basic` 关闭 |
| 公共响应头 | HSTS / nosniff / X-Frame-Options / Referrer-Policy / Permissions-Policy / CSP 均实测存在 |
| 证书剩余 | 约 **42 天**（到期前需确认自动续期成功） |

## 12. 调度配置与实际运行 — OBSERVED

2026-11-15 06:08:52 启动日志：

> 定时采集已启动：交易日 15:05（收盘后补采 17:30 / 兜底 19:30 / 终局 22:35，成分股预取
> 22:00 / 22:30 / 次日 08:30；**集合竞价 09:15–09:26 每分钟 + 09:31 补量比**；告警 20:00；
> 盘中B1 14:50；备份 23:10）

- 竞价任务真实注册并执行成功（11-13 09:15–09:31 有成功日志）；
- `auction_snapshot` 共 **139,485 行**，按日 5,583–5,584 行，最新 **2026-11-13**；
- 最近 8 天非 ok 任务：`lhb partial ×5`、`lhb_institution partial ×5`、`members empty ×10`、
  `etf skipped ×10`、`sentiment partial ×1`。

⚠️ **目标版本把竞价默认关闭，但生产当前仍在真实采集竞价**。本轮不修改。

## 13. 防火墙与出站策略 — OBSERVED

| 项 | 实测 |
| --- | --- |
| ufw | active；仅放行 22 / 80 / 443 |
| `iptables -S OUTPUT` | **`-P OUTPUT ACCEPT`**（无主机级出站限制） |
| nft/ufw 出站链 | 只有放行/记账，无 deny |
| 应用层出站 guard | **未部署**：`external_guard.py`、`egress_policy.py`、`deploy/s6/egress-policy.json` 均不存在 |
| 结论 | G3/G4 / N01–N24 继续 `NOT_VERIFIED`；生产出站**实际开放**，无第二道网 |

## 14. 外部供应商依赖 — OBSERVED

生产代码中的出站调用点（全部**硬编码域名**，未发现用户可控 URL）：

- iFinD：`api-mcp.51ifind.com`
- 东财：`push2his.eastmoney.com`、`finance.eastmoney.com`
- 腾讯：`qt.gtimg.cn`
- 同花顺：`data.10jqka.com.cn`
- 蛋卷：`danjuanfunds.com`
- 上交所 / 深交所：`query.sse.com.cn`、`fund.szse.cn`
- 开盘红：`apphwshhq.kaipanhong.com`、`apphis.kaipanhong.com`
- 通达信：`www.tdx.com.cn`
- 飞书 webhook（`FEISHU_WEBHOOK_URL`）
- 本机 KPH：`127.0.0.1:5000`（可选）

`requests` 调用均带 timeout（30–120s 级别），但**没有统一 allowlist 与私网/元数据地址拦截**。

## 15. 日志与磁盘 — OBSERVED

| 项 | 实测 |
| --- | --- |
| journal | 472.4 MB |
| `/var/log` | 515 MB |
| data 目录 | 11 GB |
| 根盘剩余 | 36 GB |
| fupan 日志轮转 | **无**（`/etc/logrotate.d` 只有 nginx） |
| 监控代理 | **无**（无 node_exporter / Prometheus / Zabbix / netdata） |

## 16. 健康与告警现状

| 检查 | 实测 |
| --- | --- |
| `http://127.0.0.1:8000/api/health` | **200**，`{"status":"ok"}` |
| `https://zhifujing.top/api/health` | **200** |
| `/api/ready`（本机） | **401** —— 生产该版本**没有 readiness 路由**，401 来自登录关卡 |
| 告警渠道 | 仅应用内 20:00 飞书收盘告警；**无主机/5xx/延迟/资源告警** |

## 17. 本轮结论（探测层）

```text
production_access = YES (read-only)
os_resources = OBSERVED / healthy
current_version = NOT_AVAILABLE (no git, blob not in history)
schema_compatible_with_target = NO (rps_daily 缺 4 列)
db_mode = SINGLE WRITABLE (no split)
db_integrity = ok
backup_recent = ok (2026-11-13, SHA256 recorded) / offsite = NO
tls = valid until 2026-12-27
scheduler = running / includes real auction collection
host_egress_control = NONE OBSERVED
app_egress_guard = NOT DEPLOYED
monitoring = NOT_AVAILABLE
probe_failed_items = 0
```
