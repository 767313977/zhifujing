# S6.11 健康检查设计与现状审查

- 时间：2026-11-15
- 生产实测：`production-readonly-probe.md` §16
- 代码审查：`backend/app/readiness.py`、`backend/app/main.py`、`deploy/setup_nginx.sh`
- **本文件只给设计与影响分析，未修改任何代码**（按任务书 §5：先评审后实施）

## 1. 现状

### 1.1 生产（2026-11-08 部署包）

| 端点 | 实测 | 说明 |
| --- | --- | --- |
| `GET /api/health` | **200** `{"status":"ok"}` | 公开、最小响应；支持 HEAD |
| `GET /api/health?detail=1` | 仅内网可带排查字段 | 公开时被降级为最小响应 |
| `GET /api/ready` | **401** | 生产该版本**没有 readiness 路由**，401 来自登录关卡 |

### 1.2 目标版本（当前仓库）

`readiness.py` 的门禁是三态字符串，`ready=true` 要求**所有 required 门禁 PASS**：

| 门禁 | 目标环境结果 | 原因 |
| --- | --- | --- |
| `STARTUP_CONFIG_VALID` | 取决于启动记录 | `FUPAN_ENV` / `SCHEMA_MODE` / 服务端预热 |
| `DATABASE_READ_ONLY_VERIFIED` | 拆分时 PASS，单库时 FAIL | 需要 `mode=ro` + `query_only` |
| `EGRESS_APP_POLICY_LOCKED` | 需要 guard 已安装 | `external_guard.install` |
| `EXTERNAL_FETCH_DISABLED` | **生产必然 FAIL** | 生产必须开启外部取数 |
| `EGRESS_OS_POLICY_VERIFIED` | **恒为 NOT_VERIFIED** | 本机无法访问 OS/网络层 |
| `SCHEDULER_DISABLED` | 生产必然 FAIL | 生产必须开调度 |
| `CANARY_READINESS` | FAIL | 上述组合 |

因此目标版本的 `/api/ready` **在任何生产配置下都会 503**。这不是故障，而是把
“灰度安全门禁”和“服务就绪”混在同一个端点上了。

## 2. 问题定义

| 问题 | 影响 |
| --- | --- |
| readiness 混入灰度/安全审计门禁 | 生产永远 503，监控无法使用 |
| 缺少纯存活端点（独立于健康） | 反向代理/监控只能用 `/api/health` 或 401 的 `/api/ready` |
| `/api/health` 与依赖状态耦合不足 | 无法区分“进程活着但数据库坏了” |
| 没有统一的依赖检查 | 数据库损坏、schema 漂移只能等页面报错 |

## 3. 建议设计（待评审）

### 3.1 `GET /api/live`（新增，公开）

- 条件：进程能处理请求（事件循环存活）即 **200**；
- 不查数据库、不查外部、不读配置；
- 响应：`{"status":"ok"}`（可选 `"version"` 构建号，不含路径/主机名）；
- 用途：nginx upstream 健康检查、黑盒拨测、`systemd` 之外的存活判据。

### 3.2 `GET /api/ready`（改造，公开但脱敏）

- **只判断“能否对外提供读服务”**：
  1. 数据库可打开并执行 `SELECT 1`（超时 1s）；
  2. schema 关键表/列存在（`schema_mode=verify` 的结果）；
  3. 必需配置有效（`SCHEMA_MODE` 合法、无启动配置错误）。
- 全部通过 → **200**；任一失败 → **503**；
- 响应只含布尔/枚举：

```json
{
  "ready": true,
  "checks": {"database": "PASS", "schema": "PASS", "config": "PASS"},
  "generated_at": "..."
}
```

- **不得**包含数据库路径、表名列表、Token、内部主机名、异常堆栈；
- **不得**无条件 200；数据库不可用时必须 503。

### 3.3 `GET /api/health`（保留 liveness，增强摘要）

- 默认仍返回最小 `{"status":"ok"}`（公开）；
- `?detail=1` 且来源为内网/本机时返回脱敏摘要：
  - `db`（PASS/FAIL）、`schema`（PASS/FAIL）、`scheduler`（running/stopped）、
  - `last_collect_date`、`version`；
  - **不返回**路径、Token、连接串、SQL、异常原文。

### 3.4 灰度/安全门禁单独保留

现有 7 个门禁**不删除、不降级**，迁移到：

- `GET /api/admin/canary-ready`（管理员）或 `GET /api/ready?strict=1`（仅内网）；
- `S6_CANARY_READY` 仍由这些严格门禁决定，**不得**因为 `/api/ready` 变绿而改变。

### 3.5 监控调用路径

| 监控项 | 端点 | 频率 |
| --- | --- | --- |
| 进程存活 | `/api/live` | 30s |
| 可服务性 | `/api/ready` | 60s |
| 业务摘要 | `/api/health?detail=1`（内网） | 5min |
| 公网入口 | `https://zhifujing.top/api/live` | 60s |

## 4. 影响分析

| 影响面 | 说明 | 风险 |
| --- | --- | --- |
| 现有测试 | `tests/test_s6_readiness*` 断言 ready 503 / 门禁名 | 需同步更新断言；严格门禁测试迁到新端点 |
| 灰度流程 | 原 `/api/ready` 是灰度开关 | 改为 `/api/admin/canary-ready`，语义不变 |
| 安全性 | `/api/ready` 公开后可能暴露“数据库是否可用” | 只返回 PASS/FAIL，不返回路径/错误；可接受 |
| nginx | 需要新增 `location = /api/live` 或直接反代 | 极小 |
| 工作量 | 1 个 readiness 重构 + 2 个路由 + 3~5 个测试 | 小 |
| 兼容 | 旧监控若用 `/api/ready` 会从 503 变 200 | 需在部署说明中注明 |

## 5. 实施前置条件（本轮未做）

1. 用户/审查确认“readiness 与服务就绪分离”；
2. 确认生产监控实际调用端点（目前没有监控系统，无历史依赖）；
3. 在隔离环境跑：`/api/live` 200、数据库断开时 `/api/ready` 503、`strict=1` 仍 503；
4. 部署时与新版本一起上线，避免生产长期停留旧行为。

## 6. 结论

```text
production_liveness = PASS (/api/health 200)
production_readiness = NOT_AVAILABLE (route absent)
target_readiness_usable = NO (always 503 by design)
design_proposed = YES (live / ready / health detail / strict gate)
implementation = NOT_STARTED (awaiting review)
health_check_readiness = BLOCKED
```
