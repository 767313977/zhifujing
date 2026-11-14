# S6 安全门禁摘要

| 项 | 结果 | 说明 |
| --- | --- | --- |
| 全局出口盘点 | PASS | 17 个出口节点；生产代码路径均有 `ensure_external_fetch` |
| 统一策略默认拒绝 | PASS | 17 条 allowlist；未知域名/端口/IP literal 拒绝；策略损坏 fail closed |
| requests / urllib | PASS | 入口 + DNS + 建连 + 写 |
| socket TCP / UDP | PASS | connect/connect_ex/send/sendall/sendmsg/sendfile |
| TLS 写 | PASS | send/sendall/sendfile/write（per-socket 嵌套） |
| DNS 入口 | PASS（应用层） | getaddrinfo/gethostbyname[_ex]；离线拒绝非回环 |
| 代理绕过 | PASS（应用层） | 系统代理/环境变量代理默认过滤为空表；回环请求强制直连 |
| 重定向 | PASS | 真实本地 302 → 外部域名被 DNS 策略拦截 |
| 凭据脱敏 | PASS | URL userinfo / query token 不进日志与异常 |
| 启动锁定 | PASS | 首次 false 永久锁定；每次 lifespan 重装 wrapper |
| 子进程（lark-cli） | **NOT_VERIFIED / 禁用** | 应用层不可控；灰度不配置 `FEISHU_TARGET` |
| OS 防火墙 / netns | **NOT_VERIFIED** | 无隔离环境；只有脚本 |
| 依赖漏洞扫描 | **NOT_VERIFIED** | 无 pip-audit / 未联网 npm audit |

## 结论

- 应用层（G2/G5/G6）通过；
- OS 层（G3/G4）未通过 → 不得灰度；
- 关键发现：系统代理（`127.0.0.1:7890`）曾使未授权请求绕过应用层直连判定，
  已通过 `_guarded_getproxies()` 修复并加回归测试。
