# S6.7 登录白屏根因修复与 G8 登录流回归验收报告

- 起始分支 / HEAD：`release/s6-canary-hardening-v3` / `9838a068a766f2fee4f46c6d7ebc6dfed4d80f0c`
- 工作分支：`fix/s6-7-login-spa-transition`
- 修复提交：`26a6621ca960aa286d00273f0d91270496601561`
- 工作区：`C:\Users\76731\Documents\trae_projects\fupan`
- 生成时间：2026-11-15（Asia/Shanghai）
- **登录缺陷：已修复并完成浏览器回归**
- **整体发布状态：仍为 `S6_CANARY_BLOCKED`**（G3/G4、24h Soak 等未通过）
- **未合并 master、未部署生产、未启用真实采集**

## 1. 状态词

```text
S6_7_LOGIN_SPA_TRANSITION_FIXED = PASS
G8_LOGIN_FLOW_REGRESSION_PASS   = PASS
S6_CANARY_READY                 = BLOCKED（G3/G4、SOAK 等仍不满足）
```

保留不变：`OS_EGRESS_DENY_NOT_VERIFIED`、`SOAK_24H_NOT_RUN`、
`PERF_SQL_BUDGET_NOT_MET`、7 条 `NOT_COMPARABLE`、
`PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED`。

## 2. 三次历史复现与本轮复现

| 来源 | 次数 | 证据 |
| --- | --- | --- |
| 上一轮 S6 验收（`reports/s6-followup/s65/browser.md`） | 3 | 登录后 `/login`、`#root` 空、刷新恢复 |
| 本轮修复前（8021，`index-icdFEUK--r2.js`） | 2 | `browser_before/evidence.md` |
| 合计 | **5** | 均有 URL / DOM / history 调用栈 |

本轮复现判定：`REPRODUCED`。修复后同一流程 10/10 通过。

## 3. 根因

`StockResearchProvider` 的「登录态变化」effect 在用户 `null → id` 时无条件调用
`writeUrl(null)`；`setSearchParams` **即使搜索串没变也会发起导航**。这次
`pushState('/login')` 与登录成功后的目标跳转在同一次提交内竞争，后发起的导航获胜，
把路由打回 `/login` 并清空 `history.state.usr`（`from` 丢失）。`Login` 随后渲染
`<Navigate>`（DOM 为 null），其 effect 依赖未变、不会重跑 → `#root` 空树、无异常。

完整 16 项候选原因逐条排除见 `root_cause_matrix.md`；状态时间线见
`auth_state_timeline_before.json`。

## 4. 修复前后状态机

```text
修复前：
AUTHENTICATED -> ROUTE_RESOLVING -> [writeUrl 干扰 pushState('/login')]
             -> ROUTE_BOUNCED -> BLANK（TARGET_ROUTE_MOUNTED 永不发生）

修复后：
AUTHENTICATED -> ROUTE_RESOLVING -> TARGET_ROUTE_MOUNTED -> CONTENT_VISIBLE
（仅当 URL 真含 stock/as_of 时才做研究参数清理）
```

## 5. 修改清单

| 文件 | 改动 |
| --- | --- |
| `frontend/src/lib/StockResearchProvider.tsx` | 账号变化时仅在含 `stock`/`as_of` 时清理 URL |
| `frontend/src/pages/Login.tsx` | `safeReturnPath()`；过期提示 Alert |
| `frontend/src/lib/authContext.ts` | `AuthValue.sessionExpired` |
| `frontend/src/lib/auth.tsx` | 401 置位 / 登录与退出清零；`RequireAuth` 传 state |
| `frontend/src/pages/__tests__/Login.test.tsx` | 补字段 + 过期提示用例 |
| `frontend/src/lib/__tests__/loginTransition.test.tsx` | 新增 4 例（修复前 3 例失败） |
| `frontend/src/lib/__tests__/routeGuards.test.tsx` | 新增 4 例 |
| `frontend/src/components/__tests__/ErrorBoundary.test.tsx` | 新增 1 例 |

未改后端认证/数据库/readiness 代码；详见 `fix_summary.md`。

## 6. 测试结果

| 项 | 结果 |
| --- | --- |
| 后端 `pytest -q` | **1735 passed / 4 skipped**（408.75s；既有 skip，未新增） |
| Ruff | All checks passed |
| compileall | 通过 |
| 前端 `npm test` | **164 passed / 19 files**（原 154，+10） |
| `oxlint` | 0 warning / 0 error（136 files） |
| `tsc -b && vite build` | 成功（vite 8.3.8） |
| G7 专项 | 29 passed；185 路由矩阵；readiness 9/9；迁移复核 PASS |
| 依赖扫描 | pip-audit：No known vulnerabilities；npm audit：0 |
| 敏感信息扫描 | 报告目录与 `9838a06..HEAD` diff 均无密码/Token/私钥/Cookie 原文 |

## 7. 浏览器 E2E（E01–E20）

PASS（17）：E01、E02、E03、E04、E05、E06、E07、E08、E09、E10、E11、E12、
E14、E17、E18、E19、E20。

NOT_RUN（3，环境限制，非缺陷）：

- **E13** 两个 Browser Context：自动化环境仅一个浏览器上下文；
- **E15** Chunk 404：无 CDP 请求拦截能力；组件级 `ErrorBoundary` fallback 已测；
- **E16** 运行时异常：无法从页面外触发 React 渲染期异常；组件级 fallback 已测。

视口（1280 / 1920 / 390）：`NOT_RUN` —— 自动化浏览器固定 982×788，
`window.resizeTo` / `window.open` 均无法改变视口，未声称移动端已验收。

连续登录：**10/10 PASS**，每轮 `path=/`、`#root=1`、`bodyLen=843`。
详见 `browser_after/summary.md`。

## 8. 安全与 G7 回归

- 非管理员：`/settings` 显示「需要管理员权限」，`GET /api/admin/status 403`；
- 站外 `from`：无外跳、无 `window.open`、无异常，落回 `/`；
- 401 失效：安全回登录页并显示「登录已过期，请重新登录。」；
- G7：Research DB 只读、User DB 可写、9 项 readiness 全 true、迁移幂等 + 恢复；
- 未放宽任何认证、授权、CSRF 或数据库边界。

## 9. Staging 部署 / 重启 / 回滚

- 命令：`python scripts/s6_staging_drill.py --snapshot 26a6621 --out-dir reports/s6_7_login/staging --keep-running`
- 结果：部署 / 重启 / 回滚 / 再部署 11 项 verdict 全 true；退出码 0；
- 再部署后浏览器登录 2/2 通过（`/`、root=1、bodyLen=393，空数据空态可见）；
- 限制：演练把当前 `frontend/dist` 同时复制到旧/新目录，**前端旧产物未真正回滚**，
  因此不能用它做「旧版白屏复现」对照（详见 `staging_rollback.log`）。

## 10. 保护资产（G0 before / final）

- C0 五脚本 blob / worktree SHA256：一致；
- 冻结信号 JSONL / meta SHA256：一致；
- 四库 size / mtime / integrity：一致；
- before/final 对比：`c0=True`、`frozen=True`、`dbs=True`，`missing=[]`、
  `integrity_failed=[]`；
- 未写入生产库、未迁移、未替换。

## 11. Git / 推送

- 分支：`fix/s6-7-login-spa-transition`
- 起始 HEAD：`9838a068a766f2fee4f46c6d7ebc6dfed4d80f0c`
- 修复提交：`26a6621ca960aa286d00273f0d91270496601561`
- 证据提交与推送状态：见 `push-status.md`（推送后生成，不参与 SHA256SUMS）
- 未 amend、未 force push、未合并 master、未部署生产。

## 12. 未关闭项

| 项 | 状态 |
| --- | --- |
| G3 / G4 OS Egress | `NOT_VERIFIED`（`S61_VM_UNAVAILABLE`） |
| 24h Soak | `NOT_RUN` |
| PERF | `PERF_SQL_BUDGET_NOT_MET`（53 > 25） |
| DATA | 7 条 `NOT_COMPARABLE` 保留 |
| E13 / E15 / E16 / 三视口 | `NOT_RUN`（环境限制，见 §7） |
| 生产部署 | `NOT_AUTHORIZED_AND_NOT_PERFORMED` |
| 前端旧产物回滚对照 | 未执行（Staging 演练产物不分版本） |

## 13. 并列准备（S6.8）

- VM 预检：`s6_8_vm_preflight.md`（Hyper-V 未启用、WSL 不可用于验收）；
- N01–N24：`n01_n24_execution_matrix.md`（全部 `NOT_RUN`，附命令/预期/证据回填 schema）；
- 未修改宿主 / WSL 防火墙、代理或 DNS。

## 14. 证据目录与校验

- 目录：`reports/s6_7_login/`
- 校验：`SHA256SUMS`（对目录内证据文件生成；不含 `SHA256SUMS` 与 `push-status.md`）
- 主要文件：`gate0_before.json`、`gate0_after.json`、`code_path_inventory.md`、
  `reproduction_matrix.md`、`browser_before/`、`root_cause_matrix.md`、
  `auth_state_timeline_before.json`、`fix_summary.md`、`test_matrix.md`、
  `browser_after/`、`backend_test.log`、`frontend_test.log`、
  `lint_typecheck_build.log`、`g7_regression.log`、`security_scan.log`、
  `staging_deploy.log`、`staging_rollback.log`、`s6_8_vm_preflight.md`、
  `n01_n24_execution_matrix.md`
