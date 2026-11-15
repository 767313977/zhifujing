# S6.7 认证/登录链路代码盘点

- 生成时间：2026-11-15（Asia/Shanghai）
- 基线：`9838a068a766f2fee4f46c6d7ebc6dfed4d80f0c`（分支 `release/s6-canary-hardening-v3`）
- 修复分支：`fix/s6-7-login-spa-transition`

## 1. 前端调用顺序（登录成功路径）

```text
Login.submit()
  -> waitForLogoutIdle()                    (lib/authContext.waitForNoLogoutInFlight)
  -> api.login()                            (api/client.request -> POST /api/auth/login)
  -> api.authMe()                           (silent401：只确认 cookie 是否还在)
  -> setUser(user)                          (lib/auth.AuthProvider.applyUser)
       -> clearPendingLogout / setSessionExpired(false)
       -> publishAuthEvent('login')         (localStorage：跨标签页)
       -> identityEpochRef++ / setMe(user)
  -> navigate(from, { replace: true })      (react-router useNavigate)
  -> Login 重新渲染：me !== null -> <Navigate to={from} replace />
  -> RequireAuth(me != null) -> 目标页面 lazy() -> Suspense -> 内容
```

关键组件层级（`App.tsx`）：

```text
BrowserRouter
  AuthProvider
    WatchlistProvider        (读登录态，登录后拉 /api/watchlist)
      StockResearchProvider  (URL ?stock= / 登录态变化同步)
        Suspense
          Routes: /login -> Login；其余 -> RequireAuth(页面)
```

## 2. 实际文件与函数

| 文件 | 关键函数 / 行为 | 与缺陷的关系 |
| --- | --- | --- |
| `frontend/src/pages/Login.tsx` | `submit()`：等待注销 → login/register → authMe 复核 → setUser → navigate；`me !== null` 时返回 `<Navigate to={from} replace />`；`safeReturnPath()`（本轮新增） | 登录成功后的目标跳转；`from` 清洗 |
| `frontend/src/lib/auth.tsx` | `AuthProvider`：启动 `authMe`、`applyUser`、`invalidateIdentity`、`identityEpochRef`、`revokeSession`、`logout`、storage 事件、401 事件；`RequireAuth` / `RequireAdmin` | 认证状态唯一来源；本轮新增 `sessionExpired` 提示状态 |
| `frontend/src/lib/authContext.ts` | `AuthContext` / `AuthValue`；logout pending、inflight、跨标签页事件 | 本轮新增 `sessionExpired` 字段 |
| `frontend/src/lib/StockResearchProvider.tsx` | `writeUrl()` → `setSearchParams`；URL→状态 effect；**登录态变化 effect** | **根因所在**：账号变化时无条件 `writeUrl(null)` 触发一次多余导航 |
| `frontend/src/lib/watchlist.tsx` | 登录后 `api.watchlist()`，失败不影响主体 | 登录后请求之一（200） |
| `frontend/src/api/client.ts` | `request()`：401 派发 `UNAUTHORIZED_EVENT`（`silent401` 除外）；`AUTH_CHECK_TIMEOUT_MS=10s`；`login/logout/authMe` | 被动失效与启动探测 |
| `frontend/src/App.tsx` | `BrowserRouter`、Provider 顺序、`Suspense`、路由守卫 | 路由与页面挂载 |
| `frontend/src/main.tsx` | `createRoot`、根级 `ErrorBoundary`、`vite:preloadError` 一次性重载 | 白屏兜底 |
| `frontend/src/components/ErrorBoundary.tsx` | `getDerivedStateFromError` + 可见 fallback | 渲染异常兜底 |
| `backend/app/api/auth.py` | `login` / `logout` / `me` / `password` | 会话建立与撤销 |
| `backend/app/services/auth.py` | `hash_password`、`resolve_session`、`require_login`（User DB） | 服务端会话权威 |
| `backend/app/main.py` | SPA fallback、`index.html` 缓存策略、静态资源 | 构建产物与路由回退 |

## 3. 浏览器实测的调用栈（修复前）

```text
replaceState("/")   <- Login 提交成功后的 navigate(from, replace)
replaceState("/")   <- Login 的 <Navigate to={from} replace />
pushState("/login") <- StockResearchProvider 的 setSearchParamsRef.current(...)
                      （账号变化 effect -> writeUrl(null) -> useSearchParams）
```

结果：URL `/login`、`#root.childElementCount=0`、body 文本 0，无异常、无失败请求。

## 4. 测试与工具入口

| 项 | 命令 / 文件 |
| --- | --- |
| 前端单测 | `cd frontend && npm test`（vitest run） |
| 新增回归 | `src/lib/__tests__/loginTransition.test.tsx`、`routeGuards.test.tsx`、`src/components/__tests__/ErrorBoundary.test.tsx` |
| 既有认证测试 | `src/pages/__tests__/Login.test.tsx`、`src/lib/__tests__/authProvider.test.tsx` |
| 后端认证测试 | `pytest tests -q`（含 auth/session/负向矩阵） |
| 拆分 Staging | `python scripts/s7_soak_split.py --serve`（127.0.0.1:8021） |
| 部署/回滚 | `python scripts/s6_staging_drill.py --snapshot <sha>` |
| G7 | `scripts/s7_route_inventory.py`、`s7_readiness_probe.py`、`s7_migration_revalidate.py` |

## 5. 未发现的事实（避免误判）

- 没有 Service Worker；没有自定义 `base`；`dist` 与 `index.html` 同源同版本；
- 登录/会话/自选接口在缺陷发生时全部 200；
- 控制台无 `console.error`、无 `pageerror`、无 `unhandledrejection`；
- `#root` 完全为空（不是 loading 骨架），说明 `Login` 走到了 `me !== null` 的
  `<Navigate>` 分支（该组件 DOM 为 null），而不是抛异常。
