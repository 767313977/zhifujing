# S6 灰度目标架构（建议）

```text
用户（内部 allowlist）
   |
   v
nginx（TLS 终止，8080/443）
   |
   v
Canary Web（uvicorn 单 worker，127.0.0.1:8000，uid=fupan）
   |-- 只读研究库副本（mode=ro）
   |-- 独立可写用户库（会话/自选/日志）      <-- 尚未实现
   |-- scheduler disabled
   |-- external data fetch disabled
   |-- 应用层 egress allowlist（已实现）
   |
   v
受控 egress proxy（域名/端口 allowlist）      <-- 尚未部署
   |
   v
OS 防火墙 / 网络命名空间（默认拒绝）          <-- 未验证
   |
   v
显式允许的外部目标
```

## 1. 已实现（本分支）

| 层 | 实现 | 证据 |
| --- | --- | --- |
| 应用策略 | `egress_policy.py` + `egress-policy.json`：默认拒绝、域名后缀+端口、IP literal 拒绝、代理默认禁用 | `security/*` |
| 应用 wrapper | requests / socket 建连与写 / TLS 写 / DNS / 代理探测 | `external_guard.py`、35 条测试 |
| 启动锁定 | `EXTERNAL_DATA_FETCH_ENABLED=false` 进程级锁定；每次启动重装 wrapper | `runtime/startup-readiness.md` |
| readiness | `/api/ready` 与 `/api/health` 分离；OS 门禁固定 NOT_VERIFIED | 同上 |
| 质量状态 | 7 条 NOT_COMPARABLE 保留；状态语义集中 | `data-quality/*` |

## 2. 未实现（阻塞灰度）

| 层 | 缺什么 | 影响 |
| --- | --- | --- |
| OS 网络策略 | 未在隔离环境加载/验证 nftables 或 netns | G3/G4 不通过 |
| 受控 egress proxy | 只有策略文件，没有代理服务 | 生产外部取数无法按 allowlist 走 |
| 数据库拆分 | 研究库与用户库同库、引擎读写 | G7 不通过；只读灰度无法登录 |
| 独立发布系统 | 无不可变镜像/负载均衡/流量切换 | 回滚演练只到「代码快照 + 配置」层 |

## 3. 明确不做的事

- 不合并 master、不部署生产、不修改生产 DNS/防火墙；
- 不启用生产 scheduler、不执行真实采集；
- 不把 `NOT_COMPARABLE`/`NOT_VERIFIED` 改成通过。
