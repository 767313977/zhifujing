# S6.7 最小修复说明

- 起点：`9838a068a766f2fee4f46c6d7ebc6dfed4d80f0c`
- 修复提交：`26a6621ca960aa286d00273f0d91270496601561`
  （`fix(login): prevent blank screen after successful sign-in`）
- 原则：只改由证据确认的路径；未做认证策略重构

## 1. 逐文件修改

| 文件 | 函数 / 位置 | 改动 | 理由 |
| --- | --- | --- | --- |
| `frontend/src/lib/StockResearchProvider.tsx` | 「登录态变化」`useEffect` | 仅在 `searchParams` 真的含 `stock` 或 `as_of` 时才 `writeUrl(null)`；依赖补 `searchParams` | 消除登录瞬间 `setSearchParams` 产生的多余 `pushState('/login')`，避免取消目标跳转 |
| `frontend/src/pages/Login.tsx` | 新增 `safeReturnPath()`；`const from = safeReturnPath(state?.from)` | 仅接受站内单斜杠路径；拒绝 `//host`、`/login` 自身 | 防开放重定向 / React Router `External navigation is not allowed` / 自跳循环 |
| `frontend/src/pages/Login.tsx` | `state` 类型、`expiredNotice` state、顶部 Alert | 被动失效时显示「登录已过期，请重新登录。」 | §4.3 会话过期必须有提示 |
| `frontend/src/lib/authContext.ts` | `AuthValue` | 新增 `sessionExpired: boolean` | 区分「主动退出」与「服务端 401」 |
| `frontend/src/lib/auth.tsx` | `AuthProvider` | 401 事件置 `sessionExpired=true`；`applyUser` / `logout` 清零；取值传入 context | 提示状态唯一来源 |
| `frontend/src/lib/auth.tsx` | `RequireAuth` | `Navigate` 的 state 增加 `sessionExpired` | 登录页能读到失效原因 |
| `frontend/src/pages/__tests__/Login.test.tsx` | `makeAuth` / 新增用例 | 补字段 + 过期提示用例 | 契约测试 |
| `frontend/src/lib/__tests__/loginTransition.test.tsx` | 新增 | 3 个修复前失败、修复后通过的回归用例 + 双击用例 | 锁住白屏、return URL、外站地址 |
| `frontend/src/lib/__tests__/routeGuards.test.tsx` | 新增 | AUTH_UNKNOWN / anonymous / authenticated / 非管理员 fail closed | §5.2 状态与权限 |
| `frontend/src/components/__tests__/ErrorBoundary.test.tsx` | 新增 | 渲染异常必须显示可见 fallback | 白屏兜底 |

## 2. 修复工作清单（§4.2）状态

| 项 | 状态 | 证据 |
| --- | --- | --- |
| 明确 unknown/loading/authenticated/anonymous/error 语义 | ALREADY_SAFE | `checking` + `me`；`routeGuards.test.tsx` |
| 明确 session restore 与 profile hydration 完成条件 | ALREADY_SAFE | `authMe → applyUser`；慢接口测试 |
| 消除多个 Store/Context 对认证状态的矛盾维护 | ALREADY_SAFE | 仅 `AuthProvider` 持有 `me` |
| 路由守卫仅在初始化完成后决策 | ALREADY_SAFE | `RequireAuth` 的 `checking` 分支 |
| 登录后目标 URL 验证 | **CHANGED** | `safeReturnPath` |
| profile/permission 失败显示错误并可重试 | ALREADY_SAFE | 登录后 `authMe` 失败提示；403 提示页 |
| 旧请求 Abort/generation guard | ALREADY_SAFE | `identityEpochRef` |
| Chunk 加载失败可见 ErrorBoundary | ALREADY_SAFE（组件）/ 浏览器未验证 | `ErrorBoundary.test.tsx`；`main.tsx` preloadError |
| 登录提交防重入 | ALREADY_SAFE | `busy` 禁用 + 双击用例（`api.login` 仅 1 次） |
| logout/account switch 清理缓存与用户级状态 | ALREADY_SAFE | 既有实现 + 账号切换浏览器实测 |
| 异步步骤成功/失败/超时三条路径 | ALREADY_SAFE | `authMe` 10s 超时；登录错误 Alert |
| 保持 API 契约 | **CHANGED（无契约变化）** | 未改任何 API 请求/响应 |

## 3. 未改动的边界（明确声明）

- 未改 `backend/app/api/auth.py`、`services/auth.py`、`db.py`、`readiness.py`；
- 未改 G7 的 Research 只读 / User 可写策略；
- 未关闭 ProtectedRoute、未跳过角色检查、未放宽 401/403；
- 未新增外部请求、未关闭 Egress 策略。

## 4. 修复前后兼容性

- `AuthValue` 新增字段属于**前端内部 context 契约**；唯一手动构造它的测试已同步，
  生产代码只有 `AuthProvider` 一处提供；
- `Login` 的 `state` 类型扩展为可选字段，旧路由 state（只有 `from` / `logoutFailed`）
  仍然兼容。
