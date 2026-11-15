# S6.9 故障注入矩阵

- 状态：**NOT_RUN**（24h Soak 未启动）
- 原因：S6.8 D 包 OS Egress 未通过；任务书要求 D 通过后才开始 E 包。
- 说明：S6.4 的 3 分钟短窗故障注入（SIGTERM/SIGKILL/User DB 只读/Research DB 缺失/锁争用）
  已完成，但**不计入** S6.9 24h 矩阵。

| # | 故障 | 目标 | 预期 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | 应用正常重启 | 服务恢复、ready 重新计算 | 无数据损坏 | NOT_RUN |
| 2 | 应用强制终止后恢复 | SIGKILL 后重启 | 事务完整、会话可重建 | NOT_RUN |
| 3 | DB 连接短暂中断 | User/Research DB | 有界错误、恢复后可读 | NOT_RUN |
| 4 | 锁争用/锁超时 | User DB | 有界失败，不死锁 | NOT_RUN |
| 5 | 网络断开/恢复 | 客户端/API | 可见错误状态，恢复后可加载 | NOT_RUN |
| 6 | DNS 不可用/恢复 | VM 内 DNS | fail closed，恢复后可用 | NOT_RUN |
| 7 | 虚拟磁盘受控降容 | VM 磁盘 | 有界告警，不填满宿主 | NOT_RUN |
| 8 | 日志写入异常 | 日志目录 | 不影响业务或明确降级 | NOT_RUN |
| 9 | worker 子进程退出 | 采集/后台 | 不静默成功 | NOT_RUN |
| 10 | 登录/登出/会话过期 | 认证 | 安全回登录页并提示 | NOT_RUN |
| 11 | Staging 版本回滚/再部署 | 版本 | 实际 serving 切换 | NOT_RUN |
| 12 | VM 重启后 Egress 持久 | nftables | 规则仍生效 | NOT_RUN |

## 前置条件

- A：已通过（E13/E15/E16/三视口）
- B：已通过（OLD/NEW manifest 不同，serving 切换 OLD→NEW→OLD→NEW）
- D：**未通过**（无 VM，N01–N24 NOT_RUN）
- G7/G0：见回归与 Gate0 final

因此本矩阵不启动，避免把“未执行”写成“通过”。
