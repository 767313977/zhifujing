# S6.7 根因矩阵

- 缺陷：`LOGIN_SPA_TRANSITION_BLANK`
- 现象：登录接口全部成功，页面停在 `/login`，`#root` 为空，刷新后恢复
- 结论：**登录成功后的目标跳转被一次无关的 URL 同步导航取消**

## 1. 必查根因矩阵（§3.2 十六项）

| # | 候选原因 | 证据 | 是否排除 | 结论 |
| --- | --- | --- | --- | --- |
| 1 | 登录 API 成功但前端认证状态未更新 | `POST /api/auth/login 200` → `setUser` 执行；`fupan.auth.event={type:login}` 写入 | 排除 | 状态已更新，问题在之后的导航 |
| 2 | Token/Cookie 写入与读取策略不一致 | 同一 Cookie 罐；`GET /api/auth/me 200`、`GET /api/watchlist 200` | 排除 | HttpOnly Cookie 正常 |
| 3 | 导航早于 profile/permission hydration | `submit` 内先 `authMe` 复核再 `setUser`；登录页无 profile 接口 | 排除 | 顺序正确 |
| 4 | 路由守卫把 unknown 错判 | 缺陷时 URL 仍是 `/login`，`RequireAuth` 未参与；页面是 `Login` 的 `<Navigate>` | 排除 | 守卫未误判 |
| 5 | 登录回调与全局认证初始化竞争 | `identityEpochRef` 已使旧 `authMe` 失效；本轮补测慢 `authMe` 通过 | 排除 | 竞态已有保护 |
| 6 | 重复挂载/副作用导致状态覆盖 | 同一提交内出现 2 次 `replaceState("/")` 与 1 次 `pushState("/login")` | **不排除** | 副作用触发了多余导航 |
| 7 | lazy import / Chunk / Suspense 永久 pending | 缺陷时 `#root` 子节点 0（不是 loading 骨架）；无失败请求；刷新后同一 chunk 正常 | 排除 | 不是 Suspense |
| 8 | 异常被吞 / ErrorBoundary 无 fallback | 无 `console.error`、无 `error`/`unhandledrejection`；ErrorBoundary 单测可见 fallback | 排除 | 没有抛异常 |
| 9 | return URL 非法、跳转循环或错误清洗 | 默认 `from="/"`；缺陷时 `location.state.usr=null`（被 pushState 清除） | 部分相关 | pushState 丢失 state，导致 `from` 回退为 `/` |
| 10 | Service Worker / 缓存旧 HTML 与新 Chunk | 无 SW；硬刷新后入口哈希一致；无 chunk 失败 | 排除 | 不是缓存 |
| 11 | 前后端构建版本/base path 不一致 | 同一 `index-DnphHmJw-r2.js` 在 8010/8021 一致；`base` 默认 | 排除 | 构建一致 |
| 12 | CORS/SameSite/Secure/CSRF/代理 Header | 同源 `127.0.0.1`；登录与后续请求均 200 | 排除 | 不是 Cookie 属性 |
| 13 | profile/permission 401/403 无失败分支 | 登录后 `authMe 200`；失败注入测试显示错误 UI | 排除 | 分支存在 |
| 14 | 请求取消/卸载/旧请求 finally 覆盖新状态 | 无相关请求失败；`finally` 只重置 busy | 排除 | 不是请求竞态 |
| 15 | 多账号切换旧请求覆盖新会话 | 账号切换实测通过；epoch 机制 | 排除 | 不是账号隔离 |
| 16 | 其他实测发现 | **见下**：`StockResearchProvider` 的账号变化 effect 无条件 `writeUrl(null)` | 命中 | 根因 |

## 2. 根因（状态机层面）

```text
AUTHENTICATED（setUser 已提交）
  -> ROUTE_RESOLVING（Login.navigate("/")，replaceState("/")）
  -> [干扰] StockResearchProvider 账号变化 effect -> writeUrl(null)
       -> useSearchParams.setSearchParams -> navigate("?", { replace:false })
       -> pushState("/login")（当时 router 仍读到旧 location）
  -> 后发起的导航获胜：路由回到 /login
  -> Login 重新渲染 me!==null -> <Navigate to="/">（DOM=null）
  -> Navigate 的 effect 依赖 [navigate,to,replace,state,relative] 未变化，不会重跑
  -> TARGET_ROUTE_MOUNTED 永不发生 -> #root 空树
```

- **哪个状态转换错误**：`AUTHENTICATED → ROUTE_RESOLVING` 被 `→ /login` 覆盖，
  `TARGET_ROUTE_MOUNTED` 从未发生。
- **触发条件**：从 `/login` 正常登录，且 URL 上没有 `stock` / `as_of` 参数；
  `StockResearchProvider` 的「登录态变化」effect 在用户态 `null → id` 时运行。
- **为什么是白屏而不是 loading/error**：`Login` 在 `me !== null` 时返回
  `<Navigate>`，该组件渲染为 `null`；目标导航被取消后 effect 不会重跑，
  所以既没有 loading，也没有异常。
- **为什么此前可复现 3 次**：条件完全确定（默认返回路径 + 无研究参数），
  每次登录都会触发；本轮浏览器复现 2 次（共 5 次证据）。
- **证据**：
  - 浏览器 history 调用栈：`replaceState("/")` ×2，随后
    `pushState("/login")`，调用方为 `StockResearchProvider` 的
    `setSearchParamsRef.current`（`Object.current`）；
  - 单元测试 `loginTransition.test.tsx` 修复前 3/3 失败（`/login` 空树、return URL 丢失）；
  - 修复后同一测试 4/4 通过，浏览器 10/10 登录通过。

## 3. 修复改变的不变量

1. 身份变化时**只有 URL 真的带研究参数**才清理 URL，避免登录瞬间的无关导航；
2. `Login` 的 return URL 只接受站内单斜杠路径，拒绝 `//evil.example` 与 `/login` 自身
   （防开放重定向与自跳循环）；
3. 被动 401 失效时登录页显示「登录已过期，请重新登录。」（不改变鉴权判定）。

## 4. 安全性证明

- 未移除/弱化任何 API 鉴权、角色检查或 CSRF 逻辑；
- 未把 401/403 改成成功；
- 未放宽 Research DB 只读 / User DB 可写边界；
- 非管理员 `/settings` 仍 fail closed（UI 提示 + `GET /api/admin/status 403`）；
- 站外 `from` 被拒绝且无外跳（无 `location.assign` / `window.open`）。
