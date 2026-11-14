# S5-D 网络栈与出网路径清单

- 生成日期：2026-11-14（Asia/Shanghai）
- 范围：`backend/app/**`、`scripts/**`、`jobs/**`（只读静态审查，不发起请求）
- 结论先行：进程内已覆盖 `requests` 与 `socket` 两层；子进程 / 原生库 / 宿主网络策略**未做 OS 级验证** → `OS_EGRESS_DENY_NOT_VERIFIED`。

## 1. 依赖与直接 HTTP 客户端

| 栈 | 出现位置 | 是否被进程内闸门覆盖 | 说明 |
| --- | --- | --- | --- |
| `requests` | `sources/tencent.py`、`kaipanhong.py`、`ifind.py`、`etf_exchange.py`、`tdx.py`、`kph_local.py`、`danjuan.py`、`ths_limit_up.py`、`eastmoney.py`（push2his）、`jobs/push_brief.py`（飞书 webhook） | 是（`requests.Session.request` 层） | 每个来源入口另有 `ensure_external_fetch` |
| `urllib3` | `sources/ifind.py`（显式 import，用于连接池/异常类型） | 是（底层用 requests/socket） | 不单独发请求 |
| `akshare` | `sources/akshare_source.py`，以及 `em_news.py` / `em_events.py` 通过 `_call_bounded` 委托 | 是（`_call_bounded` 内 `ensure_external_fetch` + requests/socket） | akshare 内部大量 `requests.get/post` |
| `urllib` / `http.client` | 应用代码无直接调用；`urllib` 出现在测试/脚本 | 是（socket 层，已用 `urllib.request` 实测拦截） | 不同网络栈验证路径 |
| `httpx` | 仅测试依赖（FastAPI TestClient）；应用运行时不使用 | 是（socket 层；`httpcore` 同步后端使用 `socket.create_connection`） | 未实际发请求 |
| `aiohttp` | 未使用 | N/A | 依赖清单中不存在 |
| WebSocket 客户端 | 应用运行时不使用；`websocket-client` 仅用于本地 Chrome CDP 验收脚本 | N/A（不属于业务出口） | 浏览器验收走回环，放行 |
| 裸 `socket` | `external_guard.py` 自身；无业务直连 | 是（`socket.create_connection` / `socket.socket.connect`） | 回环放行 |
| `subprocess` | `jobs/push_brief.py` 调用 `lark-cli` 发送飞书消息 | **否**（子进程可自行出网） | 离线验收/S5 未调用；需 OS 隔离或保持 `SCHEDULER_ENABLED=false` |
| 原生库 / 动态加载 | 未发现业务网络库；`pandas`/`numpy` 不主动出网 | 未验证 | 归入 `OS_EGRESS_DENY_NOT_VERIFIED` |
| 本地代理 / 回环服务 | `sources/kph_local.py` 访问本机开盘红服务 | 是（来源入口 + socket；回环放行） | 离线验收未启用 |

## 2. 来源入口闸门覆盖（`ensure_external_fetch`）

| 文件 | 入口 | 闸门位置 |
| --- | --- | --- |
| `sources/tencent.py` | `fetch_spot` | 发请求前 |
| `sources/kaipanhong.py` | `KaipanhongSource._post` | 发请求前 |
| `sources/ifind.py` | `IfindClient.call` / `_download_csv` | 发请求前 |
| `sources/akshare_source.py` | `_call_bounded` | 调用 akshare 前 |
| `sources/em_news.py` / `em_events.py` | 新闻 / 公告 / 研报 | 经 `_call_bounded` 委托 |
| `sources/eastmoney.py` | `fetch_daily`（push2his） | `ENABLE_PUSH2HIS` 开关之后、发请求前 |
| `sources/etf_exchange.py` | ETF 份额 | 发请求前 |
| `sources/tdx.py` | 通达信盘后包 | 发请求前 |
| `sources/kph_local.py` | 本地开盘红服务 | 发请求前 |
| `sources/danjuan.py` | 蛋卷 | 发请求前 |
| `sources/ths_limit_up.py` | 同花顺涨停原因 | 发请求前 |
| `jobs/push_brief.py` | 飞书 webhook | **仅 requests 层**；无来源入口闸门（非行情数据，且离线时不启动） |

## 3. 三层防护语义

1. **来源入口**：`ensure_external_fetch(context)` → `ExternalFetchBlocked`；不重试（`retry_call` 只尝试一次）。
2. **进程内网络层**：`install_network_deny()` 同时 patch `requests.Session.request`、`socket.create_connection`、`socket.socket.connect`；允许 `127.0.0.1` / `::1` / `localhost`。
3. **进程启动锁定**：首次读到 `EXTERNAL_DATA_FETCH_ENABLED=false` 即 `locked_offline=true`，运行中改回 true 也不会放行。

计数语义：

- `network_attempts` / `network_blocked`：只统计 `requests` 层；
- `os_network_attempts` / `os_network_blocked`：socket 建连层（回环也计尝试）；
- 二者都**不是**全宿主网络调用总数。

## 4. 未验证 / 明确保留的边界

- **OS 级出网禁止**：未使用容器 / Windows 防火墙 / 网络命名空间做实测 → `OS_EGRESS_DENY_NOT_VERIFIED`；
- **子进程**：`lark-cli` 可自行出网，进程内 socket patch 管不到；
- **原生库**：未发现，但未做穷尽证明；
- **DNS**：`socket.getaddrinfo` 未拦截（只拦截实际建连），DNS 查询本身可能出网；
- 因此本报告**不宣称**「全面防出网已验证」，只宣称「本进程 Python 网络栈 + 来源入口在离线模式下被拦截，且有实测证据」。
