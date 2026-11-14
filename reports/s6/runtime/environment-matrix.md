# S6-B1 环境配置矩阵

| 配置 | Development | Staging（本轮本地隔离） | Production Canary（建议） | 代码事实 / 默认值 |
| --- | --- | --- | --- | --- |
| `EXTERNAL_DATA_FETCH_ENABLED` | 可显式开启 | **false** | **false** | 默认 true；读到 false 后进程级锁定 |
| `SCHEDULER_ENABLED` | 按需 | **false** | **false** | 默认 true；生产 systemd 目前固定 true → 灰度必须改 false |
| 竞价采集 | 手动 | **false** | **false** | `REAL_AUCTION_DATA_NOT_VERIFIED` |
| Tushare / iFinD / 腾讯请求 | 受控 | **禁用**（`EXTERNAL_DATA_FETCH_ENABLED=false`） | **禁用** | 由 `ensure_external_fetch` + 允许名单双重约束 |
| 研究 API | 可用 | 可用（只读） | 只读开放 | 需登录（app 级依赖） |
| 数据库 | 开发库 | **临时副本** | **只读副本 + 独立用户库** | 当前引擎硬编码 `sqlite:///path`（读写）→ 见 `database-isolation-report.md` |
| Debug 路由 | 无 | 无 | 无 | `/docs`、`/redoc`、`/openapi.json` 已关闭 |
| Egress policy | 可观测 | 强制 allowlist | **OS 级强制**（本轮 NOT_VERIFIED） | `deploy/s6/egress-policy.json` |
| 错误详情 | 开发可见 | 脱敏 | 脱敏 | `ExternalFetchBlocked` 只回 reason code |
| `ENABLE_DEV_CORS` | true | false | false | 生产 systemd 已设 false |
| `FUPAN_SCHEMA_MODE` | migrate | `verify` | `verify` | 生产默认 verify（不改库） |
| `WARMUP_PATTERN_CACHE` | true | false（建议） | false | 生产开启会被 readiness 记为配置错误 |
| `FEISHU_TARGET`（lark-cli） | 可配 | **不配** | **不配** | 子进程出口应用层管不到 |
| `FEISHU_WEBHOOK_URL` | 可配 | 不配 | 不配 | 避免灰度期外呼 |

## 1. 危险开关的默认值

| 开关 | 默认 | 缺失时的行为 | 是否 fail closed |
| --- | --- | --- | --- |
| `EXTERNAL_DATA_FETCH_ENABLED` | `true` | 在线 + 允许名单（不是无条件放行） | 部分：允许名单保证未知目标拒绝；但「默认 true」本身不是 fail closed |
| `SCHEDULER_ENABLED` | `true` | 定时采集启动 | **否** → 灰度必须显式设 false |
| `FEISHU_PUSH_ENABLED` | 由 settings 决定 | 可能推送 | 灰度应显式关闭 |
| Egress policy 文件 | 仓库内置 | 缺失 → 全部拒绝 | 是 |

**结论**：生产灰度必须通过 systemd 显式设置 `EXTERNAL_DATA_FETCH_ENABLED=false`、
`SCHEDULER_ENABLED=false`、`FEISHU_PUSH_ENABLED=false`，不能依赖默认值。
`/api/ready` 会持续返回 503 直到 OS 门禁有真实证据。

## 2. 配置冲突规则

| 冲突 | 处理 |
| --- | --- |
| `FUPAN_ENV=production` 且 `ENABLE_DEV_CORS=true` | readiness `STARTUP_CONFIG_VALID=FAIL` |
| `FUPAN_ENV=production` 且 `WARMUP_PATTERN_CACHE=true` | readiness `STARTUP_CONFIG_VALID=FAIL`（避免未确认的重查询） |
| 策略文件损坏 + 外部取数开启 | 所有外部请求 `EGRESS_POLICY_ERROR`；readiness 失败 |
| 安全开关运行期修改 | `EXTERNAL_DATA_FETCH_ENABLED` 只能 online→offline；反向需重启 |
| 通过 API 修改安全开关 | **不存在此类路由**（已检索 `/api/admin`） |

## 3. 启动日志（不含密钥）

`main.lifespan()` 打印：`EXTERNAL_DATA_FETCH_ENABLED` 有效值、`offline_locked`、
`SCHEDULER_ENABLED`、`WARMUP_PATTERN_CACHE`、`FUPAN_SCHEMA_MODE`、
出站策略 `loaded/version/entries/loopback_any_port/error`。
iFinD 配额日志只打印「token 是否已配置」，不打印 token。
