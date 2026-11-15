# S6.9 Soak 事件日志

- 24h Soak：未启动（`SOAK_24H_NOT_RUN`）。
- 短时 Smoke（3 分钟）：无中止条件触发；无 5xx、无传输错误、DB integrity `ok`。
- 事件：无。
- 备注：`/api/ready` 503 为 OS Egress 门禁未验证的预期状态，不计为事件。
- 不把短时数据拼接成 24h；24h 需独立计时与完整监控。
