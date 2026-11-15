# S6.8 A 包：浏览器残余项验收（E13 / E15 / E16 / 三视口）

- 时间：2026-11-15（Asia/Shanghai）
- 执行方式：`node scripts/s6_8_cdp_e2e.mjs`
  - 真实 Chrome（headless=new）+ DevTools Protocol；未引入 Playwright/Cypress 等新框架
  - Staging：`http://127.0.0.1:8021`（隔离拆分库）
  - DEV fixture：`http://localhost:8030`（Vite dev，仅 E16/视口错误 fallback）
- 被测构建：工作区 S6.8 版本（含 S6.7 登录修复 + 本轮 chunk 自愈限流/DEV fixture）
  - dist manifest：`082da7f1bcc8ece9d9bbf102073d95a7fccf2432fe5891ac80f7e9e36a568876`
  - 文件数：59；详见 `browser/build_manifest.json`
- 结论：**`S6_8_BROWSER_RESIDUALS_PASS`**

## 1. E13 双 Browser Context

`Target.createBrowserContext` 建立两个独立 Cookie 罐：

| 项 | Context A | Context B |
| --- | --- | --- |
| 登录账号 | `s7staging`（管理员） | `s7userb`（普通） |
| `GET /api/auth/me` | id=1 / is_admin=true | id=2 / is_admin=false |
| 首页 DOM | root=1、bodyLen>200 | root=1、bodyLen>200 |
| 并行刷新后身份 | 仍 A | 仍 B |

- A `POST /api/auth/logout` 后：B 的 `/api/auth/me` 仍 200；
- A 切换为 B：A 的 `/api/watchlist` 返回 `[]`，页面显示 0 只，无 A 的残留；
- 证据：`browser/e13_context_isolation.json`、`screenshots/e13-context-a.png` / `e13-context-b.png`。

## 2. E15 真实 Chunk 404

- 拦截真实 lazy chunk：`Research-mNIyYi5C-r2.js`（Fetch.requestPaused → `Fetch.failRequest`）；
- 第一次失败触发 `vite:preloadError` → 自动重载 1 次；
- 第二次仍失败（10 秒窗口内）→ 不再重载，根级 ErrorBoundary 显示：
  「页面加载失败 / 重新加载」；
- 恢复请求后点击「重新加载」→ `/research` 正常渲染（bodyLen=205，无 fallback）；
- 失败请求 2 次，恢复后无残留；
- 证据：`browser/e15_chunk_404.json`、`screenshots/e15-chunk-404-fallback.png` / `-recovered.png`；
- 说明：旧实现每次启动清标记，持久 404 会无限刷新；本轮改为 10 秒限流，
  单测 `src/lib/__tests__/chunkReload.test.ts`（4 项）已锁住规则。

## 3. E16 React 渲染异常

- DEV-only 路由 `/__e2e/render-error`（`import.meta.env.DEV` 才挂载，生产构建无此路由）；
- fixture 在 render 阶段 `throw new Error('E2E_RENDER_ERROR_FIXTURE')`；
- 真实 Chrome 显示根级 ErrorBoundary：
  「页面加载失败…E2E_RENDER_ERROR_FIXTURE…重新加载」；
- 随后访问 `/login` 正常，证明异常未污染其它路由；
- 证据：`browser/e16_render_error.json`、`screenshots/e16-render-error.png`；
- 组件单测 `ErrorBoundary.test.tsx` 保留，但不代替本次浏览器实测。

## 4. 三视口（真实 Emulation）

| 视口 | 实际 inner | 登录页 | 首页内容 | 导航链接 | 退出 | 错误 fallback | 横向溢出 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1280×800 | 1280×800 | PASS | PASS（bodyLen 923） | 12 | PASS | PASS | 无 |
| 1920×1080 | 1920×1080 | PASS | PASS | 12 | PASS | PASS | 无 |
| 390×844 | 390×844 | PASS | PASS | 菜单展开后 ≥3 | PASS | PASS | 无 |

- 截图由 `Page.captureScreenshot` 直接生成，PNG 尺寸与请求视口一致：
  `1280×800` / `1920×1080` / `390×844`；
- 证据：`browser/viewports.json`、`screenshots/viewport-*.png`。

## 5. Console / pageerror

- E13：两 Context 均无未解释异常；
- E15：仅注入 404 导致的 `Cannot read properties of undefined (reading 'default')`
  与 ErrorBoundary 的 `页面渲染失败` 日志，均由注入故障解释；
- E16：仅 fixture 的预期渲染异常；
- 三视口：无未解释 console error。

## 6. 未使用/未伪造

- 未把组件单测冒充浏览器 E2E；
- 未在页面里留下可远程触发的故障开关（E16 仅 DEV）；
- 未记录 Cookie/Token/密码。
