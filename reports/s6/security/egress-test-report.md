# S6-A5 出站安全测试报告（应用层）

## 1. 执行结果

```
.venv\Scripts\python.exe -m pytest tests/test_s6_egress_boundary.py -q
35 passed in 2.74s
```

既有闸门回归：

```
.venv\Scripts\python.exe -m pytest tests/test_external_guard.py tests/test_s5_offline_gates.py -q
91 passed, 1 skipped in 3.62s
```

逐条结果见 `egress-test-matrix.json`（E01–E28）。

## 2. 本轮新增/修改的安全控制

| 控制 | 位置 | 说明 |
| --- | --- | --- |
| 统一允许名单 | `backend/app/egress_policy.py` + `deploy/s6/egress-policy.json` | DEFAULT=DENY；域名后缀 + 端口 + 协议 + 环境；IP literal 除回环外拒绝；文件缺失/坏 JSON/版本不符 → 全拒绝 |
| DNS 入口闸门 | `external_guard._guarded_getaddrinfo` / `gethostbyname[_ex]` | 解析前判策略；离线拒绝非回环；解析结果登记为后续 `connect(ip)` 的白名单 |
| 已连接 socket 写 | `_assert_stream_peer_allowed` | 在线也只放行回环或**受控解析登记过**的 IP；send/sendall/sendmsg/sendfile 全覆盖 |
| TLS 写 | `_guarded_ssl_send/sendall/sendfile/write` | 与 S5-R4 的 per-socket 嵌套栈兼容 |
| 代理出口 | `_guarded_getproxies` | 策略未授权代理时，把系统代理 / 环境变量代理过滤为空表；回环请求强制直连 |
| 指标口径 | `external_guard.metrics()` | `UNKNOWN` 与 `0` 严格区分；子进程/OS 防火墙固定 `UNKNOWN` |
| readiness | `backend/app/readiness.py` + `/api/ready` | 与 `/api/health` 分离；OS 门禁固定 `NOT_VERIFIED` |

## 3. 覆盖矩阵（任务书 §6 要求）

| 路径 | 覆盖方式 | 结论 |
| --- | --- | --- |
| requests / urllib3 | 单元（E01）+ redirect 集成（E20） | PASS |
| urllib.request | 单元（E02） | PASS |
| httpx | 依赖探测（E03）；未安装 | PASS（N/A） |
| aiohttp | 未安装；asyncio resolver 路径未测 | **NOT_VERIFIED** |
| websocket / websockets | 依赖存在；其底层仍是 socket + asyncio resolver | **部分 PASS / NOT_VERIFIED** |
| socket.connect / connect_ex | 单元（E11/E12/E19） | PASS |
| socket.send / sendall / sendmsg / sendfile | 单元（E04–E07） | PASS |
| TLS send / sendall / sendfile / write | 单元（E08–E10） | PASS |
| UDP | 单元（E13） | PASS |
| IPv4 | 单元（E11） | PASS |
| IPv6 | 单元（E12） | PASS |
| DNS（getaddrinfo / gethostbyname） | 单元（E14/E15） | PASS（应用层） |
| 代理（环境变量/系统代理） | 单元（E22 + system proxy 过滤） | PASS（应用层） |
| 重定向 | 集成（E20） | PASS |
| 子进程 | 证据测试（E16）证明未覆盖 | **NOT_VERIFIED** |
| curl/wget | 仅存在性检查（E17） | **NOT_VERIFIED** |
| 原生程序 | 无隔离环境（E18） | **NOT_VERIFIED** |
| 服务重启后的 OS 规则 | 未执行（E26） | **NOT_VERIFIED** |

## 4. 关键发现（本轮实测）

1. **系统代理绕过**：开发机存在 Windows 系统代理（`127.0.0.1:7890`）。在未加代理过滤前，
   `requests` 访问未授权域名会先连本机代理，应用层只看到「回环放行」，
   真实公网访问由代理进程完成 —— 测试 E20 因此最初**未被拦截**。
   修复：`_guarded_getproxies()` 在策略未授权代理时返回空表；回环请求强制直连。
   修复后 E20 通过（`example.com` 在 DNS 入口被拒）。
2. **DNS 之前未拦截**：S5 的 `dns_guard=NOT_INTERCEPTED` 已关闭；现在
   `stats()["dns_guard"] == "APP_LAYER_INTERCEPTED"`。
3. **子进程是真实缺口**：`lark-cli` 推送路径不受 wrapper 约束，灰度必须禁用。

## 5. 结论

- 应用层（G2）在本轮可验证范围内 **PASS**。
- OS 级（G3）与 DNS/IPv4/IPv6/子进程/原生库（G4）**不通过**：
  没有真实 OS/网络层证据，见 `os-policy-verification.md`。
- 因此不得标记 `S6_CANARY_READY...`；状态为 `S6_IMPLEMENTATION_PARTIAL` +
  `OS_EGRESS_DENY_NOT_VERIFIED` + `S6_CANARY_BLOCKED`。
