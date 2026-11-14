# S6.1 脚本与策略审计

审计对象：`deploy/s6/apply-egress-deny.sh`、`rollback-egress.sh`、
`verify-egress.sh`、`snapshot-egress-state.sh`、`egress-deny.nft`、
`egress-policy.json`。

## 1. 审计结论表

| 审计项 | 旧实现 | 现实现（本轮） |
| --- | --- | --- |
| 默认拒绝 OUTPUT | 仅对应用 uid 的 output 链 drop | 保留；新增防御性 `output_deny` 链（priority 10） |
| loopback 例外 | `oifname "lo" accept`（**整段放行，可连任意本机代理端口**） | 精确放行 `127.0.0.1:3128/8000` 与 `127.0.0.53:53` |
| IPv4 / IPv6 | `inet` 表，双栈 | 保留，双栈 |
| TCP / UDP | 只 drop，无显式协议分支 | 默认 drop 覆盖全部协议；DNS 精确放行 |
| 宽泛 ACCEPT / 通配网段 | 无 | 无 |
| 代理绕过 | 未处理本机任意端口代理 | loopback 端口收敛，验证脚本覆盖 `HTTP_PROXY/ALL_PROXY/本机 9999` |
| 幂等 | 直接 `nft -f`，表已存在会失败 | 先 `nft delete table inet s6_egress` 再重建，只影响本表 |
| 策略缺失 fail closed | 应用层 readiness 有；OS 层无 | OS 规则缺失时验证脚本直接拒绝运行；应用 readiness 仍为 `NOT_VERIFIED` |
| 回滚 | 删除独立表 | 保留；只删 `inet s6_egress`，不 flush |
| 回滚自动通道 | 无 | `systemd-run --on-active=15min` 自动回滚（可关闭） |
| 修改路由/DNS/系统服务 | 无 | 无（快照脚本只读） |
| 隔离守卫 | 仅 `S6_ISOLATED_ENV=1` | `S6_ISOLATED_VM=1` + root + 拒绝 WSL + 拒绝 `/mnt/c`、`/mnt/d` 挂载 + `systemd-detect-virt` 检查 |

## 2. 关键修复

1. **loopback 端口收敛**：旧规则整段放行 loopback，应用可连接本机 7890 等任意代理，
   由代理进程出网 —— 这是真实的绕过路径。现在只放行受控 proxy(3128)、后端(8000)
   与 resolver(53)。
2. **隔离守卫**：新增 WSL/宿主盘/虚拟化类型检查，避免误在日常环境执行。
3. **计数器证据**：`verify-egress.sh` 每次尝试前后读取 nft drop 计数；
   只有「连接失败 + 计数器增加」才判 PASS，避免把「没有路由/没有 DNS」误判为防火墙生效。
4. **IPv6 诚实标记**：无默认路由时输出 `NOT_VERIFIED`，不写成 PASS。
5. **快照与自动回滚**：先保存路由/DNS/防火墙/代理/namespace 基线并计算 SHA256；
   应用后可自动定时回滚。

## 3. 仍未解决 / 未验证

- 未在真实隔离 VM 加载规则（见 `environment.md`）；
- allowlist 的受控 proxy 尚未部署，`allowlist_via_proxy` 只能在有 proxy 的环境执行；
- OS 层「策略缺失 fail closed」无法由 nftables 自身保证，需要部署流程强制
  （readiness 已把 OS 门禁固定为 `NOT_VERIFIED`）；
- 子进程/原生库是否继承规则依赖 `meta skuid`，需在真实 VM 实测。

## 4. 静态测试

`tests/test_s6_egress_scripts.py`（10 passed）验证上述安全属性与 `bash -n` 语法。
