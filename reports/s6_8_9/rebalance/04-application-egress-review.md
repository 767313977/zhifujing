# S6.8 应用层出站与 SSRF 审查（平衡方案必做）

- 时间：2026-11-15
- 范围：后端服务端 HTTP client、DNS、子进程、Webhook、代理、URL 输入面
- 结论：**应用层默认拒绝 + allowlist + 私有/元数据地址阻断已具备；OS 层仍未验证**
- 本轮新增修复：DNS 解析结果只允许 pin **公网 IP**，阻断私有网段、link-local、
  CGNAT、保留地址与云 metadata（`169.254.169.254`）

## 1. 出站目的地清单（固定/配置，非用户输入）

| 出口 | 位置 | 目标 | 是否用户可控 |
| --- | --- | --- | --- |
| iFinD MCP | `backend/app/sources/ifind.py`、`config.ifind_base_url` | `api-mcp.51ifind.com:8643` | 否（配置） |
| iFinD CSV | `sources/ifind.py:_download_csv` | 由 iFinD 响应中的 URL | 否（供应商响应） |
| 东财 K 线 | `sources/eastmoney.py:_URL` | `push2his.eastmoney.com` | 否（常量） |
| 腾讯快照 | `sources/tencent.py:_SPOT_URL` | `qt.gtimg.cn` | 否（常量） |
| 同花顺涨停 | `sources/ths_limit_up.py:URL` | `data.10jqka.com.cn` | 否（常量） |
| 通达信包 | `sources/tdx.py:PACKAGE_URL` | `www.tdx.com.cn`（日期格式化） | 否（日期） |
| 开盘红 | `sources/kaipanhong.py:HOST_*` | `*.kaipanhong.com` | 否（常量） |
| 本机 KPH 代理 | `sources/kph_local.py`、`config.kph_local_base` | `127.0.0.1:5000` | 否（配置） |
| ETF 交易所 | `sources/etf_exchange.py:SSE_URL/SZSE_URL` | `query.sse.com.cn` / `fund.szse.cn` | 否（常量） |
| 蛋卷估值 | `sources/danjuan.py:BASE` | `danjuanfunds.com` | 否（常量） |
| akshare | `sources/akshare_source.py` | 第三方库内部固定供应商 | 否（库内常量） |
| 飞书 webhook | `jobs/push_brief.py:_post_card`、`config.feishu_webhook_url` | `*.feishu.cn` / `*.larksuite.com` | 否（环境变量） |
| lark-cli | `jobs/push_brief.py:_send_via_lark_cli` | 本机子进程 | 否（argv，非 shell） |

**用户输入面**：未发现接受任意 URL/目标的 API；`schemas.py` 中的 `url` 字段是
新闻/公告/研报的**响应字段**，不是请求参数。未发现文件上传接口。

## 2. 应用层控制（代码级）

| 控制 | 实现 | 结论 |
| --- | --- | --- |
| 默认拒绝 | `egress_policy`：`default=deny`，未列出目标/端口/协议拒绝 | PASS |
| 域名后缀 allowlist | `host_allowed`：精确或 `.suffix` 匹配；IP literal 禁止入名单 | PASS |
| IP literal | 只允许严格回环；其它需先由受控 DNS 登记 | PASS |
| DNS 闸门 | `getaddrinfo/gethostbyname*` 先判策略再解析；离线阻断 | PASS |
| **DNS rebinding** | 仅 pin `is_global` 地址；私有/link-local/CGNAT/保留/组播/metadata 不 pin | **本轮修复** |
| 重定向 | 初始 URL 判策略；重定向后的 DNS/connect 再判一次 | PASS（注释 E20 + 连接层） |
| 代理绕过 | 未显式授权时清空系统/环境代理；回环强制直连 | PASS |
| 离线锁 | `EXTERNAL_DATA_FETCH_ENABLED=false` 进程级锁定，socket/DNS/requests 全拦 | PASS |
| 日志脱敏 | 只记录 method/host/port；URL/query/userinfo 不进日志 | PASS |
| 子进程 | lark-cli 使用 argv 列表，`shell=False`，target 来自配置 | PASS |
| 超时 | 主要 `requests` 调用带 timeout（webhook 30s、行情 3–10s 等） | PASS（akshare 内部例外） |

新增测试：`tests/test_s6_app_egress_ssrf.py`（7 passed）

- `is_public_ip` 拒绝 127/10/192.168/172.16/169.254.169.254/100.64/0.0.0.0/::1/fd00/fe80/::ffff:10.0.0.5；
- 私有/元数据地址即使被 DNS 返回也不会进入 authorized IP 集合；
- `_guard_outbound` 对私有/元数据 literal 抛 `EGRESS_DESTINATION_DENIED`；
- 已连接 socket 的私有 peer 被 `NETWORK_STREAM_SEND_BLOCKED` 拒绝；
- 公网已授权 IP 仍可放行。

## 3. 残余风险

| ID | 风险 | 影响 | 建议 | 状态 |
| --- | --- | --- | --- | --- |
| APP-01 | `loopback.allow_any_port=true` | 若 allowlisted 供应商发生开放重定向到 127.0.0.1 任意端口，应用可能访问本机服务 | 生产策略把 loopback 端口收敛到实际依赖（5000 等）；或对供应商请求禁用自动重定向 | 记录，待发布审查 |
| APP-02 | 供应商响应中的 CSV URL | 若供应商被攻破并返回恶意主机，仍受 allowlist 限制；若返回 allowlisted 域名的内网解析，已被本轮公网 IP 限制阻断 | 保持 allowlist 最小化；CSV 下载禁用重定向更佳 | 已缓解 |
| APP-03 | akshare 内部 `requests` 无统一 timeout | 采集任务可能长时间挂起（有全局采集锁） | 采集侧加超时/熔断 | 记录 |
| APP-04 | 飞书 webhook 由环境变量配置 | 配置错误可指向任意 allowlisted 域名；不构成用户 SSRF | 部署审计 + 配置校验 | 记录 |
| APP-05 | 无 OS 层出站证据 | 应用层被绕过（新代码/原生库）时无兜底 | 保留 OS Egress 专项（DEFERRED） | `NOT_VERIFIED` |

## 4. 与 OS Egress 的边界

- 本审查**不能**替代 N01–N24；`G3_G4_OS_EGRESS_VERIFIED` 仍为 `NOT_VERIFIED`；
- 本轮未运行 `apply-egress-deny.sh` / `verify-egress.sh`，未修改任何防火墙；
- 应用层验证基于代码审查与隔离单测，未连接真实生产供应商。

## 5. 判定

```text
application_ssrf_review = PASS_WITH_RESIDUAL_RISKS
dns_rebinding_private_metadata = BLOCKED (new tests)
loopback_any_port = RESIDUAL (APP-01)
os_egress = NOT_VERIFIED (DEFERRED)
```
