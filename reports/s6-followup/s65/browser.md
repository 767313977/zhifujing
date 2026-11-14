# S6.5：真实浏览器验收（本轮）

- 环境：本地隔离拆分 Staging `http://127.0.0.1:8021`
  （Research 只读 + User 可写，外部取数/scheduler 关闭）
- 账号：`s7staging`（管理员）
- 工具：TRAE 浏览器代理（真实 Chrome，浏览器扩展注入两个 DOM 节点，不影响应用）
- 证据：本文件 + 文末缺陷复现记录（DOM / 网络 / 控制台）

## 1. 结论

| 检查项 | 结果 | 说明 |
| --- | --- | --- |
| 未登录访问受保护页 → 跳登录页 | PASS | 打开 `/` 自动落到 `/login`，表单正常 |
| 登录接口与会话 | PASS | `POST /api/auth/login 200` → `GET /api/auth/me 200` → `GET /api/watchlist 200` |
| **登录后 SPA 跳转** | **FAIL** | 提交成功后 URL 停在 `/login`，`#root` 被清空；**刷新后才进入 dashboard** |
| Dashboard 渲染（刷新后） | PASS | 涨停 40 家 · 最高 4 板；情绪卡片、涨停明细表格正常 |
| 侧边栏导航 | PASS | `/limit-up`「涨停复盘 / 晋级漏斗」、`/sectors`「板块 / 主线」正常渲染 |
| 控制台 | 无错误文本 | 但浏览器代理多次报 `Debugger is not attached`，控制台捕获不完整，不能据此断言无异常 |
| `/api/ready`（拆分形态） | PASS（语义） | 503；`DATABASE_READ_ONLY_VERIFIED=PASS`，其余门禁按真实状态 FAIL |
| `/api/admin/status`（拆分形态） | PASS | 修复前 500（`session_scope` 在只读研究库抛错），修复后 200 |

## 2. 阻断级缺陷：登录后空白（`LOGIN_SPA_TRANSITION_BLANK`）

**复现（3 轮独立复现，含一次代码改动后的复测）**

1. 打开 `http://127.0.0.1:8021`，处于未登录 → `/login`；
2. 输入 `s7staging` / `staging-password-123`，点击登录；
3. 网络：`POST /api/auth/login 200`、`GET /api/auth/me 200`、`GET /api/watchlist 200`；
4. 页面：URL 仍为 `/login`，`#root.childElementCount = 0`、`innerHTML.length = 0`、
   `body.innerText.length = 0`，等待 9 秒无变化；
5. 手动刷新 `/` → dashboard 正常渲染（正文 2 万+ 字符）。

**已排除**

- 不是后端 500：登录/会话/自检接口全部 200；
- 不是加载中：`animate-spin` / `skeleton` / 「加载中」计数为 0；
- 不是 `/api/admin/status`：该 500 已修复，刷新后的 dashboard 请求全部 200；
- 不是仅声明式跳转的问题：把 `Login.tsx` 改成只走 `<Navigate>` 后复测仍然空白，
  已回滚该实验性改动（未留在代码里）。

**当前状态**：未修复。根因尚未定位（控制台无堆栈可取证）。

**影响**：登录成功必须手动刷新才能进入站点 —— 属于阻断级 UI 缺陷，
G8 不能因此判 PASS。

## 3. 对门禁的影响

```text
G8 = FAIL（登录后跳转）
S6_FULL_REGRESSION_PASSED = FAIL
S6_CANARY_READY = 不成立
```

其余项（后端全量测试、前端单测/静态检查/构建、性能、漏洞扫描）见同目录报告。
