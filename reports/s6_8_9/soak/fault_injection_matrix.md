# S6.9 24 小时故障注入矩阵

- 24h Soak：**NOT_RUN**（未启动计时）
- 短时 Smoke 故障注入：**5/5 PASS**（见 `short_smoke_fault_matrix.md`）
- 本节列出 24h 计划中的 12 项；本轮全部未执行，不冒充通过。

| # | 故障 | 24h 状态 |
| --- | --- | --- |
| 1 | 应用正常重启 | 短时 Smoke 已覆盖；24h NOT_RUN |
| 2 | 应用强制终止后恢复 | 短时 Smoke 已覆盖；24h NOT_RUN |
| 3 | DB 连接短暂中断 | NOT_RUN（短时仅 User DB 只读/缺库/锁争用） |
| 4 | 锁争用/超时 | 短时 Smoke 已覆盖；24h NOT_RUN |
| 5 | 网络断开/恢复 | 浏览器 E2E 已做应用层模拟；24h NOT_RUN |
| 6 | DNS 不可用/恢复 | NOT_RUN（需 OS/VM 层） |
| 7 | 虚拟磁盘受控降容 | NOT_RUN（需 VM 快照） |
| 8 | 日志写入异常 | NOT_RUN |
| 9 | worker 子进程退出 | 单 worker；NOT_RUN |
| 10 | 登录/登出/会话过期 | 功能测试通过；24h NOT_RUN |
| 11 | Staging 版本回滚/再部署 | 已独立验证 PASS；24h 内 NOT_RUN |
| 12 | VM 重启后 Egress 持久 | NOT_RUN（OS Egress DEFERRED） |

## 说明

- 短时 Smoke 的 5 项故障矩阵单独记录；不把短时结果写成 24h 通过；
- 24h 启动前需完成 Release Critical 与监控/告警/停止机制。
