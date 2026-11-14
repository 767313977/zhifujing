# S6-E 本地隔离 Staging 部署记录

- 时间：2026-11-15T03:22:50+08:00
- 分支/HEAD：`release/s6-canary-hardening-v3` / `53b4b05ae2c3091eaaefe570fed7c7763025a206`
- 新版快照：`53b4b05ae2c3091eaaefe570fed7c7763025a206`（工作区快照，含未提交改动）
- 旧版快照：`HEAD`
- 新/旧代码包 SHA256：`6c9565365a1e14466148f05f0dde852d4632af8af354f221a425cc95799978b7` / `6c9565365a1e14466148f05f0dde852d4632af8af354f221a425cc95799978b7`
- 依赖锁 SHA256：requirements=`8ae8f9066862ae2091aff6b9672050bd455a2d0d2b110f6e65a43c68dba6c4e3`，package-lock=`14fad37682d6d7bf1ede83166885f7a7c826beaab7034a8ccafb8ef6dea5c206`
- 前端 index.html SHA256：`35c7a78bcbf8834566c5100ccc23ed728e8b4d327c5b19cced36a98c70751894`
- 目录：`C:\Users\76731\AppData\Local\Temp\s6_staging_5g3m8vrh\app_new` / `C:\Users\76731\AppData\Local\Temp\s6_staging_5g3m8vrh\app_old`（临时目录，演练结束可删除）
- 端口：新版 8010，旧版 8011，仅监听 127.0.0.1
- 数据库：`C:\Users\76731\AppData\Local\Temp\s6_staging_5g3m8vrh\data\staging.db`（独立临时库；登录会写该库，属预期）

## 启动配置

| 键 | 值 |
| --- | --- |
| `EXTERNAL_DATA_FETCH_ENABLED` | false |
| `SCHEDULER_ENABLED` | false |
| `WARMUP_PATTERN_CACHE` | false |
| `FUPAN_SCHEMA_MODE` | verify |
| `FUPAN_ENV` | staging |
| `ENABLE_DEV_CORS` | false |

## Smoke 结果

| 检查 | 状态码 |
| --- | --- |
| new.health | 200 |
| new.ready | 503 |
| new.unauth_quality | 401 |
| new.register | 200 |
| new.login | 200 |
| new.quality_authed | 200 |
| new.timeline_authed | 200 |
| new.health_detail | 200 |
| restart.health | 200 |
| restart.ready | 503 |
| rollback.health | 200 |
| rollback.ready | 503 |
| rollback.quality_status | 200 |
| redeploy.health | 200 |
| redeploy.ready | 503 |

**verdict：{'new_health': True, 'new_ready_503': True, 'unauth_401': True, 'register_ok': True, 'login_ok': True, 'quality_ok': True, 'restart_ok': True, 'rollback_health': True, 'rollback_ready_consistent': True, 'redeploy_ok': True, 'redeploy_ready_503': True}**

> 本记录不是生产部署：没有服务管理器、没有真实 Staging 网络、没有 OS egress 证据。
