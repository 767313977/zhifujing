# N01–N24 执行矩阵（S6.8 用；本轮全部 `NOT_RUN`）

- 目标环境：专用 Ubuntu LTS VM（非 WSL、非宿主）
- 前置：`bash deploy/s6/snapshot-egress-state.sh` 快照成功
- 应用：`S6_ISOLATED_VM=1 sudo bash deploy/s6/apply-egress-deny.sh`
- 验证：`S6_ISOLATED_VM=1 sudo bash deploy/s6/verify-egress.sh`
- 回滚：`S6_ISOLATED_VM=1 sudo bash deploy/s6/rollback-egress.sh`
- 证据：每条记录命令、退出码、原始输出、`nft list table inet s6_egress` 计数增量

| ID | 场景 | 命令（VM 内） | 预期 | 本轮 |
| --- | --- | --- | --- | --- |
| N01 | DNS UDP | `dig +time=2 +tries=1 @8.8.8.8 example.com` | 超时/被拒，counter 增长 | NOT_RUN |
| N02 | DNS TCP | `dig +tcp +time=2 +tries=1 @8.8.8.8 example.com` | 超时/被拒 | NOT_RUN |
| N03 | IPv4 TCP | `curl -4 -m 3 http://1.1.1.1` | 连接失败 | NOT_RUN |
| N04 | IPv4 UDP | `python3 -c` 发送 UDP 到 8.8.8.8:53 | 无响应 | NOT_RUN |
| N05 | IPv6 TCP | `curl -6 -m 3 http://[2606:4700::1111]` | 失败/无路由 + drop 计数 | NOT_RUN |
| N06 | IPv6 UDP | `python3` UDP `[2606:4700::1111]:53` | 无响应 | NOT_RUN |
| N07 | HTTP_PROXY | `HTTP_PROXY=http://127.0.0.1:7890 curl -m 3 http://example.com` | 失败（7890 未放行） | NOT_RUN |
| N08 | HTTPS_PROXY | 同上 `https://example.com` | 失败 | NOT_RUN |
| N09 | ALL_PROXY | `ALL_PROXY=socks5://127.0.0.1:7890 curl -m 3 ...` | 失败 | NOT_RUN |
| N10 | localhost 代理转发 | 通过 127.0.0.1:3128 访问外网 | 仅代理自身可出网；应用仍被 drop | NOT_RUN |
| N11 | Python 原生 socket | `python3 -c "socket.create_connection(('1.1.1.1',80),3)"` | 失败 | NOT_RUN |
| N12 | C 原生 socket | 编译并运行 socket 测试 | 失败 | NOT_RUN |
| N13 | TLS/SSL send | `openssl s_client -connect 1.1.1.1:443 -brief` | 握手失败 | NOT_RUN |
| N14 | sendfile | Python `os.sendfile` 到外网 fd | 失败 | NOT_RUN |
| N15 | connected socket send | 已连接 socket 发送数据 | 失败/无响应 | NOT_RUN |
| N16 | curl 子进程 | `curl -m 3 http://example.com` | 失败 | NOT_RUN |
| N17 | Python 子进程 | `subprocess.run(["curl", ...])` | 失败 | NOT_RUN |
| N18 | 原生网络工具 | `wget` / `nc` / `ping` | 失败或 ICMP 被 drop | NOT_RUN |
| N19 | allowlist 成功 | 访问策略允许的精确 loopback 端口 | 成功（3128/8000） | NOT_RUN |
| N20 | 策略缺失 fail-closed | 停用/删除策略后重启服务 | 启动失败或 ready 非 PASS | NOT_RUN |
| N21 | apply 幂等 | 连续执行 apply 两次 | 规则集一致、无报错 | NOT_RUN |
| N22 | rollback 精确 | rollback 后对比快照 | 仅删除 `inet s6_egress` | NOT_RUN |
| N23 | 重启后持久性 | reboot 后复查 | 规则仍生效（或明确记录未持久化） | NOT_RUN |
| N24 | OS 计数器证明 | `nft list table inet s6_egress` 前后 | drop counter 增量 > 0 | NOT_RUN |

## 结果回填

全部执行完后写入 `reports/s6-followup/s61/verification.json`：

```json
{
  "status": "PASS",
  "checks": {"N01_dns_udp": "PASS", "...": "..."},
  "gates": {"S61_OS_EGRESS_VERIFIED": "PASS", "G3": "PASS", "G4": "PASS"}
}
```

本轮状态：`S61_VM_UNAVAILABLE`，N01–N24 全部 `NOT_RUN`。
