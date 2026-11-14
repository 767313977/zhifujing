# S6-E 本地隔离 Staging 部署记录

- 时间：2026-11-15T01:20:06+08:00
- 分支/HEAD：`release/s6-canary-hardening-v2` / `c6db475b9848fc37e703be0bd3f186c9d20d053e`
- 新版快照：`0ae7bc6fe253be8d806c4070ad594cc75103ebae`（工作区快照，含未提交改动）
- 旧版快照：`HEAD`
- 新/旧代码包 SHA256：`56ac8277d9ffbb64f30af8f5abf970c89cc08962f2b126fb640dc630f1e3f3b8` / `e0094033bf3408a7213b2e8e3c5d8ff82ea2ce98ab1cf1a0014ed53705b124e6`
- 依赖锁 SHA256：requirements=`6725ed3ce6cb45d0470c6cba707946eba3b93d4bf5c871df22d83928738efe80`，package-lock=`549a9d35c2e3dedfec9642d90a92251baba28bb27818e51abc13f1a878ab3402`
- 前端 index.html SHA256：`c58a5266d4fac803ae78f2ccc45b9670a959e1ec643bf95442de05c602cb050e`
- 目录：`C:\Users\76731\AppData\Local\Temp\s6_staging\app_new` / `C:\Users\76731\AppData\Local\Temp\s6_staging\app_old`（临时目录，演练结束可删除）
- 端口：新版 8010，旧版 8011，仅监听 127.0.0.1
- 数据库：`C:\Users\76731\AppData\Local\Temp\s6_staging\data\staging.db`（独立临时库；登录会写该库，属预期）

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
