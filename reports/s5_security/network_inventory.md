# S5-D 网络栈与出网路径清单

- 生成日期：2026-11-14（Asia/Shanghai）
- 范围：`backend/app/**`、`scripts/**`、`jobs/**`（只读静态审查，不发起请求）
- 结论先行：进程内已覆盖 `requests`、`socket` 建连/发送、TLS `send/sendall/sendfile` 与
  `sendfile`；子进程 / 原生库 / 宿主网络策略**未做 OS 级验证** → `OS_EGRESS_DENY_NOT_VERIFIED`。

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

## 3. 三层防护语义（S5 审查整改后）

1. **来源入口**：`ensure_external_fetch(context)` → `ExternalFetchBlocked`；不重试（`retry_call` 只尝试一次）。
   `context` 只作**安全标签**：URL / path / query / userinfo 会被剔除后记录，最多 48 字符。
2. **进程内网络层**：`install_network_deny()` **启动时总是安装轻量 wrapper**
   （在线透明放行，离线按有效开关阻断），运行中 online→offline 不需要再安装：
   - `requests.Session.request`
   - `socket.create_connection`
   - `socket.socket.connect` / `connect_ex`
   - UDP `socket.socket.sendto`
   - `socket.socket.sendmsg`（显式 address 或已连接 peer；两者都没有时**阻断**）
   - 已连接 socket 的 `socket.socket.send` / `sendall`
   - `socket.socket.sendfile`（POSIX `os.sendfile` 与 Windows send fallback 都在原实现前判定）
   - `ssl.SSLSocket.send` / `sendall` / `sendfile`（TLS 子类单独 patch；`sendall` / `sendfile`
     内部回调 `send()` 时用线程局部深度避免重复计数）
   回环只允许**严格 loopback IP**（`127.0.0.0/8`、`::1` 等）；`localhost` 固定映射到
   `127.0.0.1`（`ip6-localhost` → `::1`），**不交给 OS hosts/DNS 解析**；
   `localhost.localdomain`、空 host、`None`、普通主机名一律拦截。
   已连接但 peer 不是回环的 socket：不关闭连接，但 `send` / `sendall` / `sendmsg` /
   `sendfile`（含 TLS）在发送前阻断；拿不到 peer 时 fail closed
   （`NETWORK_SEND_TARGET_UNKNOWN`）。`sendfile` 非回环阻断 reason 为
   `NETWORK_SENDFILE_BLOCKED`。
   Unix domain socket（`AF_UNIX`）不经过网络判定，保持可用。
   安装失败直接抛出，启动流程 fail closed，不会出现 `enabled=false` 却未安装拦截器的状态。
3. **进程启动锁定**：首次读到 `EXTERNAL_DATA_FETCH_ENABLED=false` 即 `locked_offline=true`，
   运行中改回 true 也不会放行（`stats().enabled` 返回**有效状态** false）。

计数语义（互不混用）：

- `attempts` / `blocked`：来源入口（`ensure_external_fetch`）；
- `network_attempts` / `network_blocked`：只统计 `requests` 层；
- `os_network_attempts` / `os_network_blocked`：TCP 建连层
  （`create_connection` / `connect` / `connect_ex`，回环也计尝试）；
- `udp_attempts` / `udp_blocked`：UDP `sendto` / `sendmsg`；
- `stream_attempts` / `stream_blocked`：已连接 socket 的 `send` / `sendall`
  （含 TLS `send` / `sendall`，以及 TCP `sendmsg`）；
- `sendfile_attempts` / `sendfile_blocked`：`socket.socket.sendfile` 与
  `ssl.SSLSocket.sendfile`（TLS 内部回调 `send()` 不重复计数）；
- `installed` / `socket_guarded`：wrapper 是否真实安装（在线也应为 true）；
- `guards`：实际安装成功的写路径能力集合
  （`requests` / `socket_connect` / `socket_stream` / `socket_sendmsg` /
  `socket_sendfile` / `ssl_stream` / `ssl_sendfile`），不只报一个总布尔值；
- `dns_guard=NOT_INTERCEPTED`：DNS 查询未在进程内拦截；
- 上述都**不是**全宿主网络调用总数。来源入口被提前拦截时，不会计入 socket 层拦截。

## 4. 未验证 / 明确保留的边界

- **OS 级出网禁止**：未使用容器 / Windows 防火墙 / 网络命名空间做实测 → `OS_EGRESS_DENY_NOT_VERIFIED`；
- **子进程**：`lark-cli` 可自行出网，进程内 socket patch 管不到；离线模式仍启动 scheduler 时，
  外部通知子进程不受本闸门保护（本轮未改调度配置）；
- **原生库**：未发现，但未做穷尽证明；
- **DNS**：`socket.getaddrinfo` 未拦截（只拦截实际建连），DNS 查询本身可能出网；
  若要求 DNS 零出网，必须配合 OS 防火墙 / 隔离网络，并另做实测；
- **平台差异**：本机 Windows 没有 `socket.socket.sendmsg`；`AF_UNIX` 与 `socket.sendfile` /
  `ssl.SSLSocket.sendfile` 存在。闸门按能力安装并在 `stats().guards` 中如实列出；
  能力缺失时测试显式断言「不假装覆盖」，Unix socket 只有在平台支持时才实测；
- 因此本报告**不宣称**「全面防出网已验证」，只宣称「本进程 Python 网络栈（requests +
  TCP/UDP socket + TLS 写 + sendfile）+ 来源入口在离线模式下被拦截，且有实测证据」。
