# S6.7 修复前复现矩阵（R01–R15）

- 修复前构建：`9838a06`，入口 `assets/index-icdFEUK--r2.js`
- 环境：本地隔离拆分 Staging `http://127.0.0.1:8021`
- 说明：本轮修复前复现使用真实浏览器（TRAE 浏览器代理）。凡被登录白屏阻断、
  无法在同一构建上继续验证的场景，如实标 `NOT_MEASURED_PRE_FIX`，不补造结果。

| ID | 场景 | 修复前结果 | 关键证据 |
| --- | --- | --- | --- |
| R01 | 未登录打开 `/login` | PASS | `#root=1`、`inputs=2`、bodyLen=74 |
| R02 | 正常登录 | **FAIL（REPRODUCED）** | 登录/会话/自选均 200；URL 仍 `/login`；`#root` 子节点 0、bodyLen 0；本轮 2 次 + 此前 3 次 = 5 次 |
| R03 | 登录后立即刷新 | PASS（仅靠刷新恢复） | 刷新 `/` 后 dashboard `#root=1`、正文 2 万+ 字符 |
| R04 | 登录后打开深层 URL | FAIL（单元级） | 修复前 `loginTransition.test.tsx` 断言 `/limit-up?days=15` 失败，实际落到 `/`；`pushState('/login')` 清空了 `history.state.usr` |
| R05 | 后退 / 前进 | NOT_MEASURED_PRE_FIX | 登录白屏阻断，无法建立已登录历史栈 |
| R06 | 退出 | PASS | 点击退出 → `POST /api/auth/logout 200` → `/login` 表单可见 |
| R07 | 退出后重新登录 | NOT_MEASURED_PRE_FIX | 会再次触发 R02 白屏 |
| R08 | 过期 / 无效会话 | NOT_MEASURED_PRE_FIX | 同上 |
| R09 | 登录 API 慢响应 | NOT_MEASURED_PRE_FIX | 同上 |
| R10 | 用户资料接口慢/失败 | NOT_MEASURED_PRE_FIX | 同上 |
| R11 | 连续双击登录 | NOT_MEASURED_PRE_FIX | 同上 |
| R12 | 无权限角色访问 | NOT_MEASURED_PRE_FIX | 需先登录成功；修复后验证 |
| R13 | Chunk 404 | NOT_MEASURED_PRE_FIX | 无 CDP 请求拦截能力；组件级 fallback 另有单测 |
| R14 | 直接访问受保护深层 URL | NOT_MEASURED_PRE_FIX | 同上 |
| R15 | 多浏览器 Context | NOT_MEASURED_PRE_FIX | 自动化环境只有一个浏览器上下文 |

## 白屏判定（不是只看 HTTP 200）

修复前记录同时满足：

```text
location.pathname == "/login"
document.querySelector("#root").childElementCount == 0
#root.innerHTML.length == 0
document.body.innerText.length == 0
可见 <a> / <nav> / 标题数量 == 0
8 秒内无 loading、无错误提示、无 console.error、无 pageerror、无失败请求
```

因此判定为**真实白屏**，而不是「加载中」或「空数据空态」。

## 复现次数与来源

| 来源 | 次数 |
| --- | --- |
| 上一轮 S6 验收（`reports/s6-followup/s65/browser.md`） | 3 |
| 本轮修复前（8021，首轮 + 加装监听复测） | 2 |
| 合计 | **5** |

修复后同一构建流程 10/10 通过，见 `browser_after/summary.md`。
