# 修复后浏览器验收（E01–E20）

- 构建：`26a6621`（入口 `assets/index-DnphHmJw-r2.js`）
- 环境：`http://127.0.0.1:8021`（隔离拆分 Staging）；部署副本 `http://127.0.0.1:8010`
- 账号：`s7staging`（管理员）与 `s7userb`（普通），均为隔离临时库
- 判定口径：`#root` 有子节点、正文有可见内容（导航/标题/表格/卡片/空态/错误提示），
  loading 不得无限持续；同时记录 console / pageerror / failed requests

## 1. 场景结果

| ID | 场景 | 结果 | 证据摘要 |
| --- | --- | --- | --- |
| E01 | 正常登录 | PASS | `/`、root=1、bodyLen=843；无异常 |
| E02 | 登录后立即刷新 | PASS | 刷新 `/` 后 root=1、bodyLen=843 |
| E03 | 登录后深层链接 | PASS | 未登录访问 `/limit-up` → `/login`；登录后回 `/limit-up`（bodyLen=514，含「涨停复盘/晋级漏斗」） |
| E04 | 后退 / 前进 | PASS | `/`→`/limit-up`→`/sectors`；back→`/limit-up`、forward→`/sectors`，均 root=1 |
| E05 | 退出 | PASS | 点击退出 → `/login`、2 个输入框 |
| E06 | 重新登录 | PASS | `/`、root=1、bodyLen=843 |
| E07 | 过期会话 | PASS | 服务端吊销后点击导航 → `/login`，显示「登录已过期，请重新登录。」，表单可用 |
| E08 | 慢登录 API（2.5s） | PASS | +600ms 时按钮「请稍候…[disabled]」、表单可见；延迟后 `/`、bodyLen=903 |
| E09 | 慢 profile API（2.5s） | PASS | +600ms 忙碌态、root=1；延迟后 `/`、bodyLen=903 |
| E10 | profile API 失败 | PASS | 停留 `/login`，显示「登录后会话校验失败…」；无「退出」按钮，root=1 |
| E11 | permission 403 | PASS | 非管理员 `/settings` 显示「需要管理员权限」；`GET /api/admin/status 403` |
| E12 | 双击登录 | PASS | 双击后单一仪表盘（banner/main/退出各 1），无重复渲染 |
| E13 | 两个 Browser Context | **NOT_RUN** | 自动化环境只有一个浏览器上下文，无法建立独立 Cookie 罐；账号隔离由 E14 证明 |
| E14 | A 切换 B | PASS | A 自选 1 条；B 登录后 `GET /api/watchlist=[]`、`/watchlist` 显示 0 只且无 000001/平安银行；切回 A 恢复 |
| E15 | Chunk 404 | **NOT_RUN（浏览器）** | 无 CDP 请求拦截；`ErrorBoundary` fallback 由单测覆盖；`main.tsx` 一次性重载策略未做浏览器级 404 注入 |
| E16 | 前端运行时异常 | **NOT_RUN（浏览器）** | 无法从页面外部让 React 渲染期抛错；`ErrorBoundary.test.tsx` 已验证可见 fallback |
| E17 | 直接刷新受保护 URL | PASS | `/limit-up` 刷新后 root=1、bodyLen=514，内容可见 |
| E18 | 外站 return URL | PASS | 注入 `usr.from='//evil.example/steal'` 后登录 → `/`；无外跳、无 `window.open`、无异常 |
| E19 | 网络断开/恢复 | PASS | API 全拒绝时 `/sectors` 显示「板块统计未取到：网络中断或超时，请重试…」且骨架完整；恢复后重新加载内容正常 |
| E20 | 连续登录 10 次 | PASS | 10/10 均为 `/`、root=1、bodyLen=843，无异常 |

## 2. 连续 10 次登录明细

| i | 结果 | path | root | bodyLen |
| --- | --- | --- | --- | --- |
| 1 | PASS | `/` | 1 | 843 |
| 2 | PASS | `/` | 1 | 843 |
| 3 | PASS | `/` | 1 | 843 |
| 4 | PASS | `/` | 1 | 843 |
| 5 | PASS | `/` | 1 | 843 |
| 6 | PASS | `/` | 1 | 843 |
| 7 | PASS | `/` | 1 | 843 |
| 8 | PASS | `/` | 1 | 843 |
| 9 | PASS | `/` | 1 | 843 |
| 10 | PASS | `/` | 1 | 843 |

每轮均先 `POST /api/auth/logout 200` + 清 storage，再做全量 `/login` 加载。

## 3. 视口

| 请求视口 | 实际 innerWidth×innerHeight | 结果 |
| --- | --- | --- |
| 1280×800 | 982×788 | 内容可见、无横向溢出（环境无法调整视口） |
| 1920×1080 | 982×788 | 同上 |
| 390×844 | 982×788 | 同上（移动端未真正验证） |

**视口状态：`NOT_RUN`（环境限制）**。当前自动化浏览器固定 982×788，
无 CDP/视口仿真工具，`window.resizeTo` 与 `window.open` 均无效。
项目本身有响应式样式，但本轮不能声称 390 宽已验收。

## 4. 控制台与网络

- 所有通过场景：`console messages: none`、`window error`/`unhandledrejection` 为 `[]`；
- 登录链路请求：`POST /api/auth/login 200` → `GET /api/auth/me 200` →
  `GET /api/watchlist 200`；
- 离线模拟场景的失败请求均为注入的 `TypeError('offline')`，恢复后无残留；
- 部署副本（8010）的空数据页返回若干 404（`/api/market/overview` 等），
  与「暂无数据，收盘后自动采集」空态一致，不是登录缺陷。

## 5. 截图

浏览器代理未落盘截图文件（本轮以 DOM / history / network 读数为证据）；
前端 UI 状态由 `body.innerText`、`#root` 结构与上述断言共同证明。
