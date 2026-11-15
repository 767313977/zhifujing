# S6.10 P0：核心业务最终验收

- 时间：2026-11-15
- 环境：隔离拆分服务，研究库 `backend/data/b1_research.db` 以 `mode=ro` 打开；
  临时用户库；`EXTERNAL_DATA_FETCH_ENABLED=false`、`SCHEDULER_ENABLED=false`
- API 证据：`core-business-acceptance.json`（`scripts/s6_10_core_business_acceptance.py`）
- 浏览器证据：`browser/history_navigation.json`（真实 Chrome，`scripts/s6_10_browser_history.py`）
- 历史浏览器证据：`reports/s6_8_9/browser/`（E13/E15/E16/三视口）
- 研究库全文件 SHA256：`a93bc080…c56b` → `a93bc080…c56b`（**未写入**）

## 1. 逐项结果

| # | 验收项 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | 用户登录 | PASS（HTTP cookie 登录 + 真实浏览器登录） | `auth_boundaries.me=200`；`home.hasLoginForm=false`、`hasExitButton=true` |
| 2 | 用户退出 | PASS | `after_logout_me=401`；三视口退出后回到 `/login`（S6.8） |
| 3 | 会话过期 | PASS（**真实过期**） | 过期会话 → 401；超 90 天绝对上限 → 401 |
| 4 | 权限检查 | PASS | 普通用户访问 `/api/admin/status` → 403；跨用户自选互不可见 |
| 5 | 首页加载 | PASS | 浏览器 `bodyLen=2855`、导航 9 链接、无 fallback |
| 6 | 个股研究 | PASS（API） | `/api/stock/000001` 200、context 200、daily 30 行 |
| 7 | 板块研究 | PASS（API） | `/api/sectors/heat` 200、`trade_date=2026-09-24` |
| 8 | 趋势股 | PASS（API） | `/api/research/trend` 200、50 行 |
| 9 | 低位启动 | PASS（API） | `/api/research/low-start` 200、50 行 |
| 10 | 板块轮动 | PASS（API） | `/api/sectors/rotation?taxonomy=kph_industry…` 200、7463 字节 |
| 11 | 情绪周期 | PASS（API） | `/api/sentiment/next-day` 200、60 组序列 |
| 12 | 数据质量页 | PASS（API） | `/api/quality/overview` 200、6 项、`as_of=2026-11-15` |
| 13 | 历史数据查询 | PASS | `market/dates` 10 天、`promotion` 15 天、`stock/daily` 30 天 |
| 14 | 刷新 | PASS（真实浏览器） | `/research` 刷新后 21,039 字符、无 fallback |
| 15 | 深层链接 | PASS（真实浏览器） | 直接打开 `/research` 即渲染 21,039 字符 |
| 16 | 浏览器前进/后退 | PASS（真实浏览器） | `/research → /watchlist → back → forward`，每一步内容正确 |

## 2. 数据真实性（不是“空数据伪装成功”）

| 检查 | 结果 |
| --- | --- |
| 研究库是否被写 | 未写（前后 SHA256 一致） |
| 是否把空列表当成功 | 否。`workbench` 明确 `rows=0` + `as_of=2026-11-13`，报告保留 0，不写成“通过” |
| 时间口径 | `market/overview`、涨停三池、板块热力均带 `trade_date`；质量页带 `as_of` |
| 计算结果与原文一致 | 抽样：涨停三池合计行数与 `promotion` 日期数一致；`stock/daily` 返回 30 行与请求 `days=30` 一致 |
| 跨用户串用 | 管理员加自选后，普通用户 `/api/watchlist` 返回 `[]` |

⚠️ **本机研究库的最新行情是 2026-09-24**，`workbench` 的 `as_of=2026-11-13` 但当天 0 行。
这是本地副本的数据新鲜度，**不代表生产**；生产数据新鲜度必须单独核对（见 §5）。

## 3. 浏览器验收细节

| 步骤 | 期望 | 实测 |
| --- | --- | --- |
| 登录后首页 | 渲染 + 导航 + 退出按钮 | `bodyLen=2855`、9 链接、`hasExitButton=true` |
| 深层链接 `/research` | 完整渲染 | `bodyLen=21039`、120 链接、无 fallback |
| 刷新 | 重载后仍完整 | `bodyLen=21039`、无 fallback |
| 站内导航 `/watchlist` | 路由切换并渲染 | `bodyLen=360`、文本含“自选池” |
| `history.back()` | 回到 `/research` | `path=/research`、文本含“研究假设” |
| `history.forward()` | 回到 `/watchlist` | `path=/watchlist`、文本含“自选池” |
| console/pageerror | 无未解释异常 | `errors=[]` |

说明：页头导航把“自选股”放在折叠菜单里，脚本在 DOM 中找不到 `/watchlist` 的 `<a>`，
因此该步用 `history.pushState + popstate` 走**同一套 SPA 路由**（不是整页跳转）；
前进/后退是真实浏览器 `history.back/forward`，每一步都校验了页面内容。

## 4. 未覆盖 / 限制

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 个股/板块页面的**浏览器级**点击 | 部分 | 本轮 API 层全覆盖；页面级见 S6.8 E13/视口，但未逐页点击所有 tab |
| 多用户并发写 | 未新增 | 复用既有 G7/并发回归结论 |
| 生产真实数据新鲜度 | `NOT_VERIFIED` | 本机副本数据停在 2026-09-24 |
| 生产登录/退出 | `NOT_VERIFIED` | 未接触生产主机 |
| 移动端菜单展开 | PASS（S6.8 三视口，390×844） | 本轮未重复 |

## 5. 上线前必须在生产补做

1. 用真实生产库检查最近交易日：`/api/market/overview` 的 `trade_date` 是否为上一交易日；
2. 登录 → 首页 → 个股 → 板块 → 趋势/低位/轮动 → 质量页 → 刷新 → 深链 → 后退；
3. 确认空数据页面显示“无数据/未采集”，而不是 0 或空白成功；
4. 用两个真实账号复核自选股/笔记不串。

## 6. 结论

```text
core_api_acceptance = PASS (19 接口全部 200，1 项认证边界组合检查)
auth_session_permission = PASS (含真实过期 401、越权 403、跨用户隔离)
browser_home_refresh_deeplink_history = PASS
research_db_untouched = PASS
data_freshness_local = STALE (2026-09-24)
data_freshness_production = NOT_VERIFIED
blocking_for_go_live = NO（代码层）/ YES（生产实据需补）
```
