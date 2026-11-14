# S6-A6 网络清单（network-inventory）

## 1. 应用监听与入口

| 组件 | 监听 | 说明 |
| --- | --- | --- |
| 后端（uvicorn） | `127.0.0.1:8000`（生产 systemd `ExecStart`） | 只监听回环，由 nginx 反代 |
| nginx | 80 / 443（配置 `DOMAIN` 时）；否则 8080 | TLS 终止点 |
| 前端静态资源 | 由 FastAPI `StaticFiles` 挂载 `/assets` + SPA 回退 | 不额外监听端口 |
| 本机 KPH 代理 | `127.0.0.1:5000`（`KPH_LOCAL_BASE`，可选） | 应用主动访问的本机服务 |

## 2. 出站目标（按用途）

见 `egress-inventory.md` / `egress-callgraph.json`。摘要：

- iFinD：`api-mcp.51ifind.com:8643`
- 东财：`*.eastmoney.com:80/443`
- 腾讯：`*.gtimg.cn:80/443`
- 同花顺：`*.10jqka.com.cn:80/443`
- 新浪：`*.sina.com.cn` / `*.sinajs.cn`
- 蛋卷：`danjuanfunds.com:443`
- 上交所 / 深交所：`*.sse.com.cn` / `*.szse.cn:443`
- 开盘红：`*.kaipanhong.com:443`
- 通达信：`*.tdx.com.cn:443`
- Tushare：`api.tushare.pro:443`
- 集思录 / 乐咕 / 中金所：`*.jisilu.cn` / `*.legulegu.com` / `*.cffex.com.cn:443`
- 飞书：`FEISHU_WEBHOOK_URL`（通常 `*.feishu.cn` / `*.larksuite.com:443`）

## 3. DNS 与代理

| 项 | 开发机实测 | 生产 |
| --- | --- | --- |
| 环境变量代理 | 全部为空 | `NOT_VERIFIED` |
| 系统代理 | **存在**：`127.0.0.1:7890`（注册表），应用层已过滤 | `NOT_VERIFIED` |
| DNS 解析 | 系统解析器 | `NOT_VERIFIED`（是否受约束未知） |
| DoH/DoT | 应用未使用 | `NOT_VERIFIED` |

## 4. 防火墙 / 网络命名空间

| 项 | 状态 |
| --- | --- |
| 生产防火墙规则 | `NOT_VERIFIED`（无主机访问权） |
| 容器 / netns 隔离 | `NOT_VERIFIED`（无容器环境） |
| 出站审计日志 | 未接入（`os_firewall_denied_total = UNKNOWN`） |

## 5. 结论

网络清单只能证明**代码要求访问哪些目标**；是否真的被限制在 OS 层，
本轮没有证据。灰度前必须在隔离环境执行 `deploy/s6/verify-egress.sh` 并保存 JSON。
