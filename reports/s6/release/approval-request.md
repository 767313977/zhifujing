# S6 灰度审批申请（Approval Request）

> 结论：**本轮不具备灰度条件**，本文件是「阻塞说明 + 需用户批准的最小动作」，
> 不是上线申请。

## 1. 本轮修复了什么

- 新增统一出站策略（默认拒绝、17 条域名+端口 allowlist、IP literal 拒绝、
  策略损坏 fail closed）；
- 新增 DNS 入口闸门与代理过滤：修复了「系统代理 `127.0.0.1:7890` 让未授权请求
  经本机代理出网」的真实绕过；
- 请求/建连/写/TLS 写/sendfile 全覆盖，已连接 socket 也在发送时复核；
- 新增 `/api/ready` 与 health 分离，OS 门禁缺失时 readiness=false；
- 修复空交易日历下质量时间线 500/400（浏览器验收发现）；
- 保留 60/60 并发回归、7 条 NOT_COMPARABLE、53 SQL 预算未达标记。

## 2. 哪些是真实 OS/网络层验证

**没有。** 本轮所有网络证据都是**应用层**（Python 进程内 wrapper + 统一策略）。
OS 防火墙 / 网络命名空间 / 子进程 / 原生库均为 `NOT_VERIFIED`。

## 3. 哪些仍未验证

- OS 级出站默认拒绝（G3）；
- DNS、IPv4、IPv6、TCP、UDP、子进程、原生库的 OS 层验证（G4）；
- 数据库只读隔离（G7，引擎仍读写）；
- 依赖漏洞扫描；
- 长时间 soak / 数据库锁争用。

## 4. 哪些功能被禁用

- 外部数据现取（iFinD/Tushare/腾讯/东财/同花顺等）；
- 后台定时采集；
- 竞价真实采集；
- 飞书推送（含 lark-cli 子进程）；
- 管理员写操作与用户写入类功能（在只读库未拆分前）。

## 5. 使用什么数据库

- 本轮 Staging：独立临时 SQLite（`%TEMP%\s6_staging\data\staging.db`）；
- 生产：**未连接、未写入、未迁移**；
- 灰度目标：只读研究库副本 + 独立可写用户库（**尚未实现**）。

## 6. 外部请求与调度器

- `EXTERNAL_DATA_FETCH_ENABLED=false`（进程级锁定）；
- `SCHEDULER_ENABLED=false`；
- 启动日志已记录策略版本与锁定状态。

## 7. 灰度用户范围

建议仅内部 allowlist（具体名单由用户提供）；本轮未创建任何生产用户或流量入口。

## 8. 如何回滚

见 `rollback-runbook.md`；本地演练已通过（代码快照 + 配置 + 数据库副本）。

## 9. 需要用户明确批准的具体动作（按顺序）

1. 批准在**隔离 Staging**（非日常开发机）执行 `deploy/s6/apply-egress-deny.sh` +
   `verify-egress.sh`，并提供结果；
2. 批准数据库拆分方案（只读研究库 + 独立用户库）后再改引擎；
3. 批准进入生产灰度前，先完成 G3/G4/G7；
4. 明确灰度用户名单、流量方式与 SLO 阈值（当前 `NOT_DEFINED`）；
5. 明确授权是否允许合并 master / 部署生产（当前**不授权**）。

## 10. 当前状态

```text
S6_IMPLEMENTATION_PARTIAL
OS_EGRESS_DENY_NOT_VERIFIED
S6_CANARY_BLOCKED
PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED
```
