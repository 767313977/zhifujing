# S6.5 浏览器验收（Staging）

- 目标：`http://127.0.0.1:8010`（本地隔离 Staging，真实 `frontend/dist`）
- 方式：真实浏览器自动化（登录表单、逐页访问、DOM 文本 + Network 状态码）
- 结果：**PASS**（3 项 Done 条件全部满足）

| 页面 | 渲染 | console error | 关键接口 |
| --- | --- | --- | --- |
| `/data-quality` | 6 来源「未采集」+ 时间线空态 | 0 | overview 200、timeline 200 |
| `/research/workbench` | 骨架正常；空库提示「交易日历为空」 | 0 | workbench 400（空日历，准确原因） |
| `/auction` | 空态「0 只有竞价数据」 | 0 | market/auction 200 |
| `/limit-up` | 空态「0 只涨停」 | 0 | promotion 200 |
| `/sentiment` | 「情绪数据未入库」 | 0 | sentiment/next-day 200 |

## 发现并修复

`/sentiment` 的「全 A 涨跌分布」在 `GET /api/market/advance-decline` 返回 404 时，
`error=null` 且 `data=null`，`LoadState` 的 `loading={error===null}` 永真，
「暂无数据」不出现。已改为显式 `settled` 状态，并新增前端回归测试
`AdvanceDeclinePanel.test.tsx`（1 passed）。

## 未落盘截图

本机 Chrome CDP 调试端口不可用，登录后页面截图无法导出；证据为 DOM/Network 记录。
登录页三视口截图沿用上一轮（`reports/s6/staging/browser/*.png`，SHA256 已记录）。
