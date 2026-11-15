# S6.10 P0：SSRF 与出站安全最终审查

- 时间：2026-11-15
- 范围：应用层（Python 进程内）出站闸门、供应商访问、akshare、子进程、代理、TLS
- 证据：代码 + `tests/test_s6_app_egress_ssrf.py`、`tests/test_s6_egress_boundary.py`、
  `tests/test_s5_offline_gates.py`（本轮 97 passed / 1 skipped，见 `../ssrf-tests.log`）
- **OS 层出站（防火墙/N01–N24）本轮仍为 `NOT_VERIFIED` / `DEFERRED`，未被本文件证明**

## 1. 结论先行

| 项 | 结论 |
| --- | --- |
| 用户可控 URL | **不存在**（所有外部目标都来自固定供应商常量或允许名单） |
| loopback 任意端口 | **已关闭**（`allow_any_port=false`，只留 5000） |
| 供应商重定向 | 逐跳按「最终 IP + 端口」重新判定，回环也按端口判定 |
| DNS rebinding | 只登记**公网 IP**；私网/链路本地/元数据地址无法成为授权 peer |
| akshare 无限等待 | 已有超时 + 在飞熔断（上限 8），超时让出采集锁 |
| 子进程出站 | 仅 `lark-cli`（可选推送），60s 超时；**进程外网络不受进程内闸门约束** |
| 代理绕过 | 默认清空系统/环境代理；显式代理一律拒绝 |
| TLS | 生产 `verify` 开启（系统 CA），不再 `verify=False` |

**应用层判定：`PASS_WITH_RESIDUAL_RISKS`。OS 层判定：`NOT_VERIFIED`。**

## 2. Loopback（重点整改项）

### 2.1 整改内容

- `deploy/s6/egress-policy.json`：
  ```json
  "loopback": { "allow": true, "allow_any_port": false, "ports": [5000] }
  ```
  仅保留本机 KPH 代理 `127.0.0.1:5000`（`kph_local_enabled` 默认 false，业务上按需开启）。
- `backend/app/external_guard.py` 新增 `_loopback_policy_allows()` / `_block_loopback()`，
  在以下入口强制按策略校验端口：
  - `socket.connect` / `connect_ex` / `socket.create_connection`（`_guard_outbound`）；
  - `requests.Session.request`（`_guarded_request`）。
- 仅“严格 IP 回环”（`ipaddress.is_loopback`）和 `localhost` / `ip6-localhost` 固定映射视为回环；
  `localhost.localdomain`、空 host、任意主机名都不会被豁免。

### 2.2 已知边界（不隐藏）

- `send` / `sendall` / `sendfile` / `write` 跑在**已建立**的连接上：对端端口是客户端临时端口，
  无法据此反推“用户是否访问了不该访问的本机端口”。因此
  `_assert_stream_peer_allowed` 对回环只校验“对端是回环”，**不校验端口**；
  端口控制在 `connect` / `request` 阶段完成（攻击面已在这两处收口）。
- 该边界已写入测试注释，未用“已完全覆盖”表述。

### 2.3 测试

- `tests/test_s6_app_egress_ssrf.py`：5000 放行、8000 拒绝、已连接流不误伤；
- `tests/test_s6_egress_boundary.py`：
  - `test_E20b_redirect_to_unlisted_loopback_denied`（重定向到未列端口）；
  - `test_E20c_direct_unlisted_loopback_port_denied`（直连未列端口）；
  - `test_E27_loopback_legit_access_allowed`（策略内端口正常）；
- `tests/test_s5_offline_gates.py`：离线时回环仍放行、TLS `send/sendall/write/sendfile` 正常。

## 3. 供应商重定向

- `requests` 的自动重定向在库内部完成，**不会回到** `_guarded_request`，因此不能只靠 URL 层；
- 每一跳最终都会走 `socket.connect` → `_guard_outbound`：
  - 主机名不在允许名单 → 拒绝（`EGRESS_DESTINATION_DENIED`）；
  - IP literal 必须是“被受控 DNS 解析登记过 + 公网 IP”；
  - 回环必须命中 loopback 端口策略；
- `test_E20_redirect_to_unauthorized_domain_denied` 验证跨域重定向被拒；
- 残余：重定向发生在 TLS 建连之后才被拒（首跳已发出请求），无法阻止“首跳到达允许域”。
  这不是 SSRF（目标是允许域），但供应商可能观察到请求。记录为残余风险。

## 4. DNS rebinding

- `_guarded_getaddrinfo` / `gethostbyname[_ex]`：先按允许名单判主机，再解析；
- `_record_authorized_ip`：**只登记 `is_global` 的 IP**；
- `_is_public_ip`：显式拒绝
  - 私网（10/8、172.16/12、192.168/16、fc00::/7）
  - 回环（127/8、::1）
  - 链路本地（169.254/16、fe80::/10）
  - CGNAT（100.64/10）
  - 保留/组播/未指定地址
  - IPv4-mapped IPv6：按内嵌 IPv4 判定
- 元数据地址 `169.254.169.254` 同时命中链路本地，`_is_global=false` → 无法登记、无法连接；
- 已授权 IP 表有上限（2048），超限清空重建，避免无界增长。

## 5. IPv4 / IPv6

- IPv4 与 IPv6 走同一套 `_is_public_ip` / `_guard_outbound`；
- 离线实测：公网 IPv6 `2606:4700:4700::1111:443` 被拦；`::1` 回环放行；
- `localhost` 固定映射到 `127.0.0.1`，不在 OS hosts/DNS 上留解释空间。

## 6. akshare 内部超时

- `backend/app/sources/akshare_source.py::_call_bounded`：
  - 守护线程执行 + `future.result(timeout)`；
  - 超时抛 `TimeoutError`，上层记 `failed` 并**释放采集锁**，不会让 worker 永久堆积；
  - 在飞熔断：`_inflight >= 8` 时不再开新线程，直接失败（防止挂死线程按天累积）；
  - `em_news` / `em_events` 等 FastAPI 侧 akshare 调用同样经 `_call_bounded`。
- 阈值：`http_timeout=60s`（iFinD 自然语言工具最慢 4.1s，留足余量）。

## 7. 子进程出站

- 全仓库仅一处 `subprocess.run`：`jobs/push_brief.py` 调 `lark-cli` 发飞书，`timeout=60`；
- 参数来自配置（`lark_cli` / 目标 id），**不接受用户输入拼接**；
- ⚠️ 子进程自己的网络不受 Python 进程内 socket wrapper 约束 —— 若 lark-cli 被替换/劫持，
  应用层闸门看不到。属于残余风险，缓解是它只在“飞书推送”路径、可选、失败不影响站点。
- 无 `os.system` / `Popen` 其它用法。

## 8. 代理环境变量

- `_guarded_getproxies()`：策略未显式列代理时返回空表；
- `_configured_proxy()`：`session.proxies` 或 `trust_env` 环境代理存在 → `PROXY_NOT_ALLOWED`；
- 回环请求强制 `proxies={"http": None, "https": None, "all": None}`，防止被本机代理劫持出公网；
- `sources/ifind.py`、`sources/eastmoney.py` 显式 `trust_env=False`。

## 9. TLS 证书校验

- iFinD：默认校验证书，生产用系统 CA（`IFIND_CA_FILE=/etc/ssl/certs/ca-certificates.crt`）；
- 全仓库无 `verify=False` / `_create_unverified_context`；
- 本地 TLS 集成测试使用 `tests/fixtures/tls` 自签名证书，不连公网、不含生产密钥。

## 10. 外部 API 超时与异常

| 来源 | 超时 | 证据 |
| --- | --- | --- |
| iFinD 主链 | `http_timeout=60s` | `sources/ifind.py` |
| iFinD CSV | `CSV_TIMEOUT` | `sources/ifind.py` |
| 东财日线 / 新闻 | `_TIMEOUT` / `_call_bounded` | `sources/eastmoney.py`、`em_news.py` |
| 腾讯实时 | `_SPOT_TIMEOUT` | `sources/tencent.py` |
| ETF 交易所 | `(10, 40)` | `sources/etf_exchange.py` |
| 通达信 | `(10, 120)` | `sources/tdx.py` |
| KPH 本地 | `kph_local_timeout=15s` | `sources/kph_local.py` |
| akshare | 外层 `_call_bounded` | `sources/akshare_source.py` |

出站被拒时统一 `503 + offline=true + reason_code`，不伪装成“没有数据”。

## 11. 残余风险

| ID | 风险 | 影响 | 缓解 | 是否阻断上线 |
| --- | --- | --- | --- | --- |
| SSRF-R1 | OS 层出站控制未验证（N01–N24） | 应用层被绕过时无第二道网 | 应用层默认拒绝 + 允许名单；上线前在主机启用 nftables 策略并复核 | **是（需明示接受或补齐）** |
| SSRF-R2 | 子进程（lark-cli）网络不受进程内闸门约束 | 仅供飞书推送；被替换可出站 | 固定可执行路径、60s 超时、推送失败不影响站点 | 否（记录） |
| SSRF-R3 | 重定向首跳已到达允许域后才拒绝后续跳 | 允许域可观察请求 | 供应商域名最小化；无用户可控 URL | 否（记录） |
| SSRF-R4 | 已建立连接的 send 不校验回环端口 | 极端情况下可向本机其它服务发数据 | 连接前已限制；无用户可控目标 | 否（记录） |
| SSRF-R5 | `_call_bounded` 超时线程无法取消 | 长期运行可能有少量挂住线程 | 熔断上限 8 + 守护线程 | 否（记录） |

## 12. 判定

```text
application_ssrf_final = PASS_WITH_RESIDUAL_RISKS
loopback_any_port = CLOSED
metadata_private_linklocal = BLOCKED
dns_rebinding = MITIGATED (authorized public IP only)
redirect_recheck = PASS (per-hop at connect)
akshare_timeout = PASS
subprocess_egress_guard = PARTIAL (documented risk)
os_egress_n01_n24 = NOT_VERIFIED / DEFERRED
```
