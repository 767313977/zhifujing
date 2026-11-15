# S6.11 最终 Go/No-Go 上线审查报告

- 时间：2026-11-15 17:00（Asia/Shanghai）
- 基线：`work/s6-8-s6-9-gate-closure`，HEAD `48819deda0bf581511b36c6420b60072f4e76d6f`
- 生产：`zhifujing.top`（腾讯云 Ubuntu 24.04.5），本轮完成**只读探测**
- **未部署、未迁移、未重启、未改配置、未开启灰度、未改 `S6_CANARY_READY`**

## 0. 最终结论

```text
GO_LIVE_DECISION = NOT_READY
```

相比 S6.10，本轮把“生产实际配置未知”这个最大盲区补上了：主机、数据库、备份、TLS、
调度、出站与监控都已实测。但实测结果暴露了新的**真实阻断项**（单库可写、schema 缺列、
无监控、无回滚基线），因此仍不能上线。

## 1. 验收条件逐项（任务书 §9）

| # | 条件 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | 生产实际配置已核实 | **PASS** | `production-readonly-probe.md`（15 项 OBSERVED） |
| 2 | 数据库架构与写入边界明确 | **FAIL（BLOCKED）** | 单库可写；普通用户可写研究表；`database-write-boundary.md` |
| 3 | 备份与恢复可靠 | **PARTIAL** | 备份 5 份 ok、SHA256 记录；**无异地**；生产未做恢复演练 |
| 4 | 健康检查可用 | **FAIL** | 生产无 `/api/ready`；目标版本恒 503；`health-check-review.md` |
| 5 | 最低监控与告警实际送达 | **FAIL** | 无监控代理；无渠道；未测试送达 |
| 6 | 上线/回滚责任人明确 | **FAIL（PENDING）** | `release-responsibility.md` 全部未指定 |
| 7 | 生产出站残余风险已评估 | **PASS（评估完成）** | 主机 `OUTPUT ACCEPT`；应用 guard 未部署；已登记 R-06/R-13 |
| 8 | G0 保护资产无异常 | **PASS** | `protected-assets-before/final.json`、`gate0-compare.json` |

结论：8 项中 **3 项 PASS、1 项 PARTIAL、4 项 FAIL** → 不能申请发布。

## 2. 本轮关键发现

1. **生产版本不可识别**：`/opt/fupan` 无 `.git`，`main.py` SHA256 与仓库 50 个历史版本
   均不匹配；部署时间 2026-11-08。没有“上一版”可回滚。
2. **单库可写**：无 `DB_SPLIT_ENABLED`，生产代码无拆分能力；普通用户可通过
   `POST /api/watchlist/sync`、`POST /api/stock/{code}/sync`、
   `GET /api/sector/{code}/members` 写入 `stock_daily` / `sector_member` / `ifind_usage`。
3. **Schema 缺列**：`rps_daily` 缺 `ret20/ret60/rps20/rps60`，目标版本 `verify` 启动会失败；
   迁移脚本幂等、可空、可回滚，但本轮禁止执行。
4. **备份同盘**：最近备份 2026-11-13 23:10（`c96b29b2…b6efd3`），5 份全部 ok，
   但日志明确“未配置异地同步”。
5. **无监控**：无 exporter/Prometheus/Zabbix；无 5xx/延迟/资源告警；无渠道。
6. **出站开放**：`iptables -P OUTPUT ACCEPT`；生产未部署 `external_guard` / allowlist。
7. **真实竞价仍在采集**：`auction_snapshot` 139,485 行，调度日志 09:15–09:31；
   目标版本默认关闭，但生产是旧代码。
8. **TLS 正常**：Let's Encrypt，有效期至 2026-12-27，`certbot.timer` active。

## 3. 从 NOT_READY 到可上线的执行顺序

按依赖排序，完成一项勾一项：

| 步骤 | 内容 | 是否需用户授权 |
| --- | --- | --- |
| 1 | 指定上线负责人/值班/告警接收/回滚决策与执行人 | **是** |
| 2 | 创建生产目录快照 `/opt/fupan-prev-*`，记录当前 `main.py` SHA256 | 是（写生产） |
| 3 | 打包目标版本，生成发布包 SHA256，记录目标 commit | 否（本地） |
| 4 | 实施 `RESEARCH_WRITE_DISABLED` 守卫（方案 A）并测试 | 是（改代码） |
| 5 | 实施 live/ready/health 拆分并测试 | 是（改代码） |
| 6 | 在维护窗口执行 `rps_daily` 迁移 + 部署 + 验收 | 是（写生产） |
| 7 | 配置异地备份并验证同步 | 是（改生产） |
| 8 | 接入监控与告警，完成一次**真实送达**测试 | 是（改生产/渠道） |
| 9 | 复核 G0、备份 SHA、回滚命令，提交独立审查 | 是 |

完成 1–8 后可评为 `GO_LIVE_READY_WITH_ACCEPTED_RISKS`；
若同时完成 OS 出站策略（N01–N24）可评为 `GO_LIVE_READY`。

## 4. 本轮未做（明确声明）

- 未部署生产、未合并 master；
- 未启用/停用调度、未启用/关闭真实采集；
- 未迁移或写入生产数据库；
- 未修改任何生产配置或服务；
- 未执行回滚演练；
- 未发送任何告警或飞书消息；
- 未修改 `S6_CANARY_READY`；
- 未停止/删除 `s6-egress-vm`（仍 Running）。

## 5. 证据索引

| 文件 | 内容 |
| --- | --- |
| `baseline-audit.md` | Git、保护资产、生产访问与脚本安全审查 |
| `production-readonly-probe.md` / `.json` | 生产 15 项实测 |
| `evidence/prod-probe-raw.txt` | 综合探测原文（exit 0） |
| `evidence/prod-probe-with-rss-raw.txt` | `prod_probe.sh` 原样执行（含 RSS 峰值） |
| `database-write-boundary.md` | 单库写入边界、迁移差异、最小修复方案 |
| `health-check-review.md` | live/ready/health 设计与影响 |
| `monitoring-alert-readiness.md` | 监控缺口与告警渠道 |
| `release-responsibility.md` | 版本、发布、回滚与责任 |
| `remaining-risk-register.md` | 风险登记 |
| `protected-assets-before/final.json`、`gate0-compare.json` | G0 |
| `SHA256SUMS` | 本目录校验和 |

## 6. 停止点

本轮到此停止，等待独立审查与用户明确发布授权。
