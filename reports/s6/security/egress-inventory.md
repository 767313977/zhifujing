# S6-A0 全局网络出口盘点

- 采集时间：2026-11-15（UTC+08:00）
- 代码基线：`release/s6-canary-hardening`（自 `104dfdf` 创建，含 `3060e82`）
- 方法：全仓库静态检索（`requests` / `urllib` / `httpx` / `aiohttp` / `websocket` /
  `socket` / `ssl` / `subprocess` / `os.system` / `getaddrinfo` / `gethostbyname`）+
  依赖清单核对 + 进程内 wrapper 动态验证。
- **限制**：静态检索不能证明「没有网络出口」；子进程、原生库与宿主网络策略见
  `os-policy-verification.md`（本轮 `NOT_VERIFIED`）。

## 1. 出口总表

| 出口 ID | 调用位置 | 协议 | 使用库 | 目标 | 用途 | 当前开关 | 现有拦截层 | 子进程可否绕过 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| EG-01 | `backend/app/sources/ifind.py:306,676` | HTTPS/TCP | requests + `urllib3` | `api-mcp.51ifind.com:8643` | iFinD MCP 行情/财务/EDB/CSV | `EXTERNAL_DATA_FETCH_ENABLED` + `SCHEDULER_ENABLED` | `ensure_external_fetch` + requests/socket/TLS/DNS wrapper + 允许名单 | 否（同进程） | 保留（仅采集） |
| EG-02 | `backend/app/sources/eastmoney.py:124,142` | HTTPS/TCP | requests | `push2his.eastmoney.com:443` | 东财日线 | 同上 | 同上 | 否 | 保留 |
| EG-03 | `backend/app/sources/tencent.py:292,301` | HTTP/TCP | requests | `qt.gtimg.cn:80` | 腾讯实时快照 | 同上 | 同上 | 否 | 保留 |
| EG-04 | `backend/app/sources/ths_limit_up.py:72,85` | HTTPS/TCP | requests | `data.10jqka.com.cn:443` | 涨停原因 | 同上 | 同上 | 否 | 保留 |
| EG-05 | `backend/app/sources/danjuan.py:64,66` | HTTPS/TCP | requests | `danjuanfunds.com:443` | 指数估值 | 同上 | 同上 | 否 | 保留 |
| EG-06 | `backend/app/sources/etf_exchange.py:56,58` | HTTPS/TCP | requests | `query.sse.com.cn:443`、`fund.szse.cn:443` | ETF 份额 | 同上 | 同上 | 否 | 保留 |
| EG-07 | `backend/app/sources/kaipanhong.py:188,194` | HTTPS/TCP | requests | `apphwshhq/apphis.kaipanhong.com:443` | 板块成员/快照 | 同上 | 同上 | 否 | 保留 |
| EG-08 | `backend/app/sources/kph_local.py:57,61` | HTTP/TCP | requests | `127.0.0.1:5000` | 本机 KPH 代理 | `KPH_LOCAL_ENABLED` | 回环策略 | 否 | 保留（本机） |
| EG-09 | `backend/app/sources/tdx.py:141,144` | HTTPS/TCP | requests | `www.tdx.com.cn:443` | 通达信盘后包 | 同上 | 同上 | 否 | 保留 |
| EG-10 | `backend/app/sources/akshare_source.py:123,227` | HTTPS/TCP | akshare（内部 requests） | 东财/同花顺/新浪/中金所/集思录/乐咕等 | 三池、龙虎榜、财务、可转债、期指 | `EXTERNAL_DATA_FETCH_ENABLED` | `ensure_external_fetch`（`_call_bounded`）+ socket/DNS/代理 wrapper | 否（同进程线程） | 保留（域名按策略） |
| EG-11 | `backend/app/sources/em_news.py:100`、`em_events.py:120,154,207` | HTTPS/TCP | akshare | 东财新闻/公告/研报/解禁 | 个股事件 | 同上 | 同上 | 否 | 保留 |
| EG-12 | `backend/app/jobs/collect_erp.py:107`、`collect_futures_*.py`、`collect_qvix.py` | HTTPS/TCP | akshare | 中金所/新浪/上交所等 | 宏观/期货/波指 | 同上 | 同上 | 否 | 保留 |
| EG-13 | `backend/app/jobs/push_brief.py:583` | HTTPS/TCP | requests | `FEISHU_WEBHOOK_URL`（`open.feishu.cn` 等） | 飞书推送 | `FEISHU_PUSH_ENABLED` | requests/socket/DNS wrapper + 允许名单 | 否 | 保留（仅推送） |
| EG-14 | `backend/app/jobs/push_brief.py:621` | 子进程 | `lark-cli`（外部二进制） | 由 CLI 决定 | 飞书推送（webhook 未配置时） | `FEISHU_TARGET` | **应用层无法覆盖** | **是** | **待确认 → 生产灰度禁用** |
| EG-15 | `scripts/forward_collect_tushare.py:1172` | HTTPS/TCP | tushare（requests） | `api.tushare.pro:443` | 前瞻研究库采集 | 手动脚本 | requests/socket/DNS wrapper | 否（同进程） | 保留 |
| EG-16 | `backend/app/main.py:517` | UDP（本机） | `socket.SOCK_DGRAM` | `127.0.0.1` / 本机地址 | 探测本机出口 IP | 启动时一次 | 回环放行 | 否 | 保留 |
| EG-17 | `backend/app/external_guard.py` 自身 | TCP/UDP/DNS | socket/ssl | 任意 | 安全闸门 | — | — | — | 控制面 |

## 2. 依赖层面的出口

| 依赖 | 是否安装 | 网络实现 | 是否被 wrapper 覆盖 |
| --- | --- | --- | --- |
| `requests 2.34.2` | 是 | urllib3 → `socket.getaddrinfo` / `socket.socket.connect` | 是（入口 + DNS + 建连 + 写） |
| `urllib3 2.8.0` | 是 | 同上 | 是（经 socket） |
| `urllib.request` | 标准库 | `http.client` → socket | 是（经 socket / DNS） |
| `httpx` | **否** | 若安装，同步后端走 `socket.create_connection` | 是（未来经 socket） |
| `aiohttp` | **否** | 若安装，asyncio → `loop.getaddrinfo` / `sock.connect` | 部分：`socket.getaddrinfo` 已接管；asyncio 的 loop 解析器需另测 → **NOT_VERIFIED** |
| `websockets 17.1` / `websocket-client` | 是 | asyncio / socket | 部分（同上） |
| `tushare 1.4.29` | 是 | requests | 是 |
| `akshare 1.18.96` | 是 | requests（多域名） | 是（同进程） |
| `subprocess` / `lark-cli` | 是 | 子进程自有网络栈 | **否** → OS 层必须兜底 |

## 3. 代理与 DNS

| 项 | 事实 |
| --- | --- |
| 环境变量代理 | 本轮开发机 `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY` 均为空 |
| 系统代理（Windows 注册表） | **存在**：实测 `requests` 会连到 `127.0.0.1:7890`（本机代理），从而绕过应用层直连判定 → 已由 `_guarded_getproxies()` 默认过滤为空表 |
| DNS 解析入口 | `socket.getaddrinfo` / `gethostbyname[_ex]` 已接管；离线拒绝非回环，在线按允许名单拒绝未知域名 |
| DoH / DoT | 应用内未使用；若第三方库自带 DoH，其 HTTPS 出口仍受域名策略约束 → 未单独验证 |
| 外部 DNS（UDP/TCP 53） | 应用层在 `getaddrinfo` 处拦截；**OS 层未验证** |

## 4. 结论

1. 生产代码路径的外部出口全部集中在 `backend/app/sources/*`、`backend/app/jobs/*`
   与 `scripts/forward_collect_tushare.py`，均经过 `ensure_external_fetch` 或
   `_call_bounded`。
2. `lark-cli` 子进程是唯一已知的**应用层不可覆盖**出口 → 灰度必须禁用
   （`FEISHU_TARGET` 不配置，或 OS 层阻断）。
3. 子进程 / 原生库 / OS 防火墙无法在本轮验证 → G3/G4 不通过，见
   `os-policy-verification.md`。
