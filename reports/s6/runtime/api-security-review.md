# S6-B5 管理与 API 安全审查

范围：认证/会话、Cookie、CORS、CSRF、限流、路由权限、错误泄露、SSRF、
SQL 参数化、路径穿越、依赖锁。**不重写认证系统**；只列事实与需要修复/禁用的点。

## 1. 认证与会话

| 项 | 事实 | 结论 |
| --- | --- | --- |
| 口令存储 | `hashlib.scrypt`（标准库，带盐） | PASS |
| 会话存储 | 服务端 `UserSession`，库里只存 token 的 SHA256 | PASS |
| Cookie | `HttpOnly`、`SameSite=Lax`、`Secure` 仅在 HTTPS 时置位 | PASS |
| 会话期限 | 30 天滑动 + 90 天绝对上限 | PASS |
| 登录/注册限流 | `guard_rate(ip)` + `guard_user_rate(username, ip)`；失败与重置共用同一键 | PASS |
| 用户枚举 | 不存在的用户也走 `DUMMY_PASSWORD_HASH`，耗时对齐 | PASS |
| 登出 | 只撤销请求自带的 token；登录成功时撤销旧会话 | PASS |

## 2. 路由与权限

| 项 | 事实 |
| --- | --- |
| 全站登录 | `app` 级依赖 `require_login`，白名单制、默认拒绝 |
| 免登录白名单 | `login / register / logout / health / ready` |
| 管理接口 | `/api/admin` 整个 router 挂 `require_admin` |
| 在线文档 | `/docs`、`/redoc`、`/openapi.json` 已关闭（历史上曾免登录泄露） |
| Debug 路由 | 未发现 |
| 运行期改安全开关 | 不存在此类 API（已检索 admin 路由） |
| 可匿名触发采集/写库 | 未发现：外部取数入口在登录后的业务路由或调度器内；`/api/admin/collect` 已删除 |

## 3. CORS / CSRF

| 项 | 事实 | 结论 |
| --- | --- | --- |
| CORS 白名单 | 仅 `localhost:5173` / `127.0.0.1:5173`，且生产 `ENABLE_DEV_CORS=false` 时为空 | PASS |
| 允许方法/头 | GET/POST/PATCH/DELETE/OPTIONS；仅 `Content-Type` | PASS |
| `allow_credentials` | 未开启 | PASS |
| CSRF | Cookie `SameSite=Lax` + 同源前端；写接口要求 JSON body | 可接受（未做 token 级 CSRF） |
| 明文 HTTP 登录/注册 | 公网 + 明文时拒绝（`_require_secure_transport`） | PASS |

## 4. 错误与信息泄露

| 项 | 事实 |
| --- | --- |
| 离线拦截 | 统一 503 + 固定文案 + reason code；不含 URL / token |
| 出站策略拒绝 | 日志只含 method + hostname + port |
| health detail | 仅内网（连接对端 IP）且只返回三个 bool |
| 堆栈 | FastAPI 默认 500 不回堆栈；日志写本地文件（轮转） |
| readiness | 只含门禁名/状态/原因码，不含路径或主机名 |

## 5. SSRF / 注入 / 路径穿越

| 项 | 事实 | 结论 |
| --- | --- | --- |
| 用户可控 URL | 未发现：数据源地址是常量；`FEISHU_WEBHOOK_URL` 来自配置而非请求 | PASS |
| 重定向跟随 | requests 默认跟随；最终目标仍走 DNS/建连策略（E20） | PASS |
| SQL 参数化 | 全部走 SQLAlchemy ORM / `text()` 参数；未见字符串拼接 SQL | PASS |
| 路径穿越 | 静态资源由 `StaticFiles` 挂载；SPA 回退对敏感前缀直接 404 | PASS |
| 文件上传 | 未发现上传接口 | N/A |

## 6. 依赖与锁文件

| 项 | 事实 |
| --- | --- |
| 后端锁 | `backend/requirements.txt`（版本固定；SHA256 见 `gate0/dependency-inventory.txt`） |
| 前端锁 | `frontend/package-lock.json` |
| 漏洞扫描 | 本机未安装 `pip-audit` / `npm audit` 未联网执行 → **`NOT_VERIFIED`** |
| 已知风险 | `starlette>=1.0.1` 为下限（防 CVE-2026-48710 回退）；`requests 2.34.2`、`urllib3 2.8.0` |

## 7. 需要在灰度前处理/禁用的点

1. `lark-cli` 子进程推送：灰度不配置 `FEISHU_TARGET`，避免应用层管不到的出口。
2. `/api/admin/*`：灰度对非 allowlist 用户不可用（管理员专属），建议灰度期禁用写操作。
3. 用户数据写入接口（journal/positions/watchlist/plans/analysis）：依赖只读库拆分，
   未完成前灰度只能开放只读研究功能。
4. 依赖漏洞扫描未执行 → 审批材料中列为 `NOT_VERIFIED`。
