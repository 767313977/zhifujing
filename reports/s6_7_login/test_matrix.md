# S6.7 测试矩阵

- 前端：`npm test` → **164 passed / 19 files**（原 154 → 新增 10）
- 后端：`pytest -q` → **1735 passed / 4 skipped**（既有 skip，未新增）
- Lint：`oxlint` 0 warning / 0 error（136 files）
- 类型检查 + 构建：`tsc -b && vite build` 成功（vite 8.3.8）

## 1. 前端状态/组件测试（§5.2 十五项）

| # | 要求 | 覆盖用例 | 结果 |
| --- | --- | --- | --- |
| 1 | AUTH_UNKNOWN 显示 loading | `routeGuards.test.tsx`「AUTH_UNKNOWN 只显示有界 loading」 | PASS |
| 2 | 初始化完成且 anonymous 跳登录 | `routeGuards.test.tsx`「未登录时带 from 跳登录页」 | PASS |
| 3 | authenticated 正常挂载 | `routeGuards.test.tsx`「已认证时才挂载」 | PASS |
| 4 | profile hydration 失败显示错误 | `Login.test.tsx`「会话校验失败不建立登录态」；浏览器 E10 | PASS |
| 5 | permission 失败 fail closed | `routeGuards.test.tsx`「非管理员 fail closed」；浏览器 E11 | PASS |
| 6 | 双击登录只触发一次有效提交 | `loginTransition.test.tsx`「快速双击只发起一次有效登录」 | PASS |
| 7 | 登录状态更新完成后再导航 | `loginTransition.test.tsx`「登录成功后渲染目标页内容」 | PASS |
| 8 | 旧会话请求晚到不能覆盖新会话 | `authProvider.test.tsx`「logout-start 之后的迟到 authMe」 | PASS |
| 9 | logout 清理缓存/inflight | `authContext.test.ts`（11 项）＋ `authProvider.test.tsx` 注销用例 | PASS |
| 10 | account switch 不显示旧用户数据 | 浏览器 E14（A/B 账号 + 自选隔离） | PASS |
| 11 | 外站 return URL 被拒绝 | `loginTransition.test.tsx`「站外 return URL 被拒绝」 | PASS |
| 12 | Chunk 失败显示 fallback | 组件级 `ErrorBoundary.test.tsx`；浏览器 404 注入未执行 | **PARTIAL** |
| 13 | ErrorBoundary 可见 fallback | `ErrorBoundary.test.tsx` | PASS |
| 14 | loading 超时进入错误状态 | 代码路径：`api.authMe` 10s 超时 → catch → 匿名；本轮未新增假时钟用例 | **PARTIAL** |
| 15 | 旧请求 finally 不清理新请求状态 | `authProvider.test.tsx` epoch 用例 + `loginTransition` 单实例断言 | PASS |

## 2. 后端认证契约（§5.3）

| 契约 | 覆盖 | 结果 |
| --- | --- | --- |
| 登录成功 / 失败 | `tests/test_audit_round2.py`、`test_audit_round3.py`、`test_audit_20261108.py` | PASS（全量 pytest） |
| 无效 / 过期会话 | 同上 + 浏览器 E07 | PASS |
| logout 后旧会话失效 | 同上 + 浏览器 E05/E07 | PASS |
| 权限不足拒绝 | 浏览器 E11（`/api/admin/status 403`） | PASS |
| CSRF / CORS / Cookie 属性 | 既有 audit 测试（未改动） | PASS |
| return URL 不由后端盲目重定向 | 前端 `safeReturnPath`；后端无该逻辑 | PASS |
| profile/permission 失败不误判 authenticated | 浏览器 E10/E11 | PASS |
| G7 Research 只读 / User 可写 | `test_s7_route_ownership.py`、`test_s7_db_negative_matrix.py`、`test_s6_db_split.py`（29 passed） | PASS |

## 3. 浏览器 E2E（§5.4）

见 `browser_after/summary.md`：E01–E12、E14、E17–E20 共 17 项 PASS；
E13（多 Context）、E15（Chunk 404）、E16（运行时异常注入）为 `NOT_RUN`，
原因分别为：自动化环境单上下文、无 CDP 请求拦截、无法从页面外触发 React 渲染期异常。
视口三档为 `NOT_RUN`（浏览器固定 982×788，无法仿真）。

> 不把组件级 fallback 测试冒充浏览器 E2E；两类结果分开报告。

## 4. 新增/修改测试文件

| 文件 | 用例数 | 性质 |
| --- | --- | --- |
| `src/lib/__tests__/loginTransition.test.tsx` | 4 | 新增（修复前 3 个失败） |
| `src/lib/__tests__/routeGuards.test.tsx` | 4 | 新增 |
| `src/components/__tests__/ErrorBoundary.test.tsx` | 1 | 新增 |
| `src/pages/__tests__/Login.test.tsx` | 5（+1） | 修改 |

未删除任何测试、未新增 skip/xfail、未放宽断言或 timeout。
