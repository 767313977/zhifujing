# S6.10 生产环境配置审计

- 时间：2026-11-15
- 方式：仓库内生产部署声明 + 本机/Staging 实配只读核对
- **未接触生产主机**：生产实际 `.env`、`systemctl cat fupan`、nginx 站点配置、
  磁盘与文件权限本轮均未采集。任何“生产已正确”的结论都标 `NOT_VERIFIED`，
  不以 Staging / 开发配置代替。
- 本文件不记录任何密码、Token、私钥、Webhook 或数据库连接串。

## 0. 三套配置必须分开看

| 环境 | 来源 | 数据库模式 | 调度 | 外部取数 | 用途 |
| --- | --- | --- | --- | --- | --- |
| **生产（声明）** | `deploy/install.sh` systemd unit | 未设 `DB_SPLIT_ENABLED` → 默认**单库可写** | `SCHEDULER_ENABLED=true` | 默认 `true` | 云端采集 + 站点 |
| **Staging / 验收** | 本轮验收脚本 | `DB_SPLIT_ENABLED=true`，研究库 `mode=ro` | `false` | `false` | 只读验收 |
| **开发（本机）** | 仓库根 `.env` | 未设 → 单库 | `SCHEDULER_ENABLED=false` | 默认 `true` | 本机调试 |

⚠️ **最重要的差异**：验收用的“研究库只读 + 用户库分离”是 **Staging 配置**；
按仓库内 `install.sh`，生产当前是**单库可写**。两者不能互相替代结论。

## 1. 生产环境变量（systemd 声明）

| 变量 | 值 | 评价 |
| --- | --- | --- |
| `PYTHONUNBUFFERED` | `1` | 日志实时 |
| `TZ` | `Asia/Shanghai` | 采集时刻/交易日判定正确 |
| `SCHEDULER_ENABLED` | `true` | 生产采集必须；与验收环境相反 |
| `AUCTION_COLLECT_ENABLED` | `false`（**本轮新增**） | 真实竞价未验收前不注册 9:15–9:26 定时器 |
| `IFIND_CA_FILE` | `/etc/ssl/certs/ca-certificates.crt` | 强制校验证书 |
| `ENABLE_DEV_CORS` | `false` | 不向生产浏览器下发开发源 |
| `HOST` / `PORT`（.env） | 被 ExecStart 的 `--host 127.0.0.1 --port 8000` 覆盖 | 不对外暴露 |

生产 `.env`（服务器手工维护）应包含 `IFIND_AUTH_TOKEN`、`FEISHU_*` 等密钥 —— 本轮**未读取**。

## 2. 数据库连接与读写权限

| 检查 | 声明/证据 | 状态 |
| --- | --- | --- |
| 生产库路径 | `backend/data/fupan.db`（默认 `db_path`） | 声明 |
| 生产是否拆分库 | `install.sh` 未设 `DB_SPLIT_ENABLED` → 单库可写 | **声明；实际 `NOT_VERIFIED`** |
| 研究库只读 | 仅 Staging 打开 `mode=ro` + `PRAGMA query_only` | Staging PASS |
| 目录权限 | `chmod 700 backend/data`；服务用户 `fupan` | 声明 |
| 启动 Schema 策略 | `schema_mode=verify`（默认），缺表/列/索引即启动失败 | 代码 PASS |
| Redis / 缓存 | 项目**无 Redis 依赖**，无外部缓存 | PASS（不适用） |

## 3. 认证 / 会话 / Cookie / CSRF

| 项 | 实现 | 状态 |
| --- | --- | --- |
| 密码存储 | `hashlib.scrypt`（n=2^15），带盐，`scrypt$n$r$p$salt$digest` | PASS（代码） |
| 会话 | 服务端 `user_session`，令牌 `secrets.token_urlsafe(32)`，库存 sha256 | PASS（代码） |
| Cookie | `HttpOnly`、`SameSite=Lax`、`https` 时 `Secure`；`max_age` 90 天 | PASS（代码） |
| 过期 | 30 天滑动 + 90 天绝对上限；每日 03:30 清理 | PASS（代码） |
| CSRF | 依赖 `SameSite=Lax` + 同源 `fetch`；**无独立 CSRF token** | 残余风险（见风险登记） |
| 明文公网登录 | `_require_secure_transport` 对“公网 + http”直接 403 | PASS（代码） |
| JWT / 静态密钥 | 不使用 JWT，无共享 HMAC 密钥 | PASS（不适用） |
| nginx Basic Auth | 默认 `off`，鉴权由后端登录承担 | 声明；实际 `NOT_VERIFIED` |

## 4. CORS / TLS / 反向代理

| 项 | 值 | 状态 |
| --- | --- | --- |
| CORS 来源 | 生产 `ENABLE_DEV_CORS=false` → 空名单 | 声明 |
| CORS 方法/头 | `GET/POST/PATCH/DELETE/OPTIONS`，仅 `Content-Type` | PASS（代码） |
| TLS | nginx + Let's Encrypt；`TLSv1.2 TLSv1.3`；HSTS 1 年 | 声明 |
| 代理头 | `X-Forwarded-Proto` 已传，`Secure` cookie 依赖它 | 声明 |
| 超时 | `proxy_read/send_timeout=600s` | 声明 |
| 安全头 | CSP / nosniff / X-Frame-Options / Referrer-Policy / Permissions-Policy | 声明 |
| 非本站 Host | 80 端口 444；443 默认块 `ssl_reject_handshake` | 声明 |
| 实际生效配置 | `nginx -T`、证书到期日 | **`NOT_VERIFIED`** |

## 5. 日志与脱敏

| 项 | 实现 | 状态 |
| --- | --- | --- |
| 日志级别 | `LOG_LEVEL=INFO`（默认） | PASS |
| 出站拦截日志 | 只记 method + host + port；URL/path/query/userinfo 不进日志 | PASS（代码） |
| 异常响应 | 503 固定文案 + reason code，不回显 URL | PASS（代码） |
| 认证日志 | 不记录密码/token；会话库只存 sha256 | PASS（代码） |
| 访问日志 | nginx `fupan_main`（含 rt/urt） | 声明 |
| 生产日志实际内容抽查 | 未采集 | **`NOT_VERIFIED`** |

## 6. 外部数据源

- 出站允许名单：`deploy/s6/egress-policy.json`（17 条，默认拒绝，按后缀 + 端口）；
- iFinD TLS 走系统 CA；`trust_env=False` 绕开环境代理；
- 详见 `security/ssrf-final-review.md`。

## 7. 任务调度与备份

| 项 | 值 | 状态 |
| --- | --- | --- |
| 调度器 | 进程内 APScheduler，**必须单 worker** | 声明（install.sh 未设 `--workers`） |
| 主采集 | 15:05；晚间 17:30 / 19:30；收尾 22:15+；成分股 22:00/22:30 | 代码 |
| 竞价 | **默认不注册**（本轮） | PASS |
| 备份 | 每日 23:10，保留 5 份，可选异地目录 | 代码 |
| 告警 | 每日 20:00 收盘链路检查（飞书） | 代码 |
| 生产实际 next_run / 最近执行 | 未采集 | **`NOT_VERIFIED`** |

## 8. 磁盘与重启策略

| 项 | 值 | 状态 |
| --- | --- | --- |
| `Restart` | `always`，`RestartSec=10`，`TimeoutStopSec=30` | 声明 |
| 文件句柄 | `LimitNOFILE=65535` | 声明 |
| sandbox | `NoNewPrivileges`、`ProtectSystem=full`、`PrivateTmp`、`RestrictAddressFamilies` 等 | 声明 |
| 磁盘余量 | 未采集；注释记载库约 1.5 GB × 5 份备份 | **`NOT_VERIFIED`** |
| 内存/CPU 余量 | 未采集（S6.9 短时 smoke 仅有本机数据） | **`NOT_VERIFIED`** |

## 9. 结论

```text
repo_declared_production_config = CONSISTENT_WITH_INTENT
staging_config_substituted_for_production = NO
production_actual_config_verified = NO (NOT_VERIFIED)
production_db_mode = UNKNOWN (repo default: single writable DB)
blocking_for_go_live = YES (需在生产主机执行只读采集并逐项确认)
```

**必须补齐的最小只读证据**（在生产主机执行，不写库、不重启）：

1. `sudo systemctl cat fupan`（脱敏后）；
2. `sudo systemctl show fupan -p ExecStart -p Restart -p MainPID -p MemoryCurrent`；
3. `sudo nginx -T | grep -n -e listen -e auth_basic -e ssl_protocols -e proxy_read_timeout`；
4. `ls -l /opt/fupan/backend/data` + `df -h`；
5. `journalctl -u fupan -n 100 --no-pager`（确认调度器与最近采集）；
6. 确认 `/opt/fupan/.env` 是否设置 `DB_SPLIT_ENABLED`（只回答“是否”，不要回传内容）。

可用 `deploy/prod_probe.sh` 一次性采集（脚本只读）。
