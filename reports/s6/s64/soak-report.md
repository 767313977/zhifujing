# S6.4 Staging 短时 soak（NOT 24H）

- 目标：`http://127.0.0.1:8010`（本地隔离 Staging，独立临时库）
- 窗口：2 分钟（任务书要求 ≥24 小时 → **未满足**）
- 请求：1,560 次；**5xx = 0**；传输错误 = 0
- 延迟：P50 3.7 ms / P95 26.5 ms / P99 27.8 ms
- `/api/ready` 503 × 195（**预期**：OS 门禁未验证）
- `/api/research/workbench` 400 × 195（**预期**：空交易日历，错误信息准确）

| 路径 | 状态码 |
| --- | --- |
| `/api/health` | 200 × 195 |
| `/api/ready` | 503 × 195（预期） |
| `/api/quality/overview` | 200 × 195 |
| `/api/quality/timeline` | 200 × 195 |
| `/api/research/workbench` | 400 × 195（空库预期） |
| `/api/auction` | 200 × 195 |
| `/api/sentiment/next-day` | 200 × 195 |
| `/api/limit/promotion` | 200 × 195 |

## 结论

**`SOAK_NOT_24H`**。短窗内无 5xx、无传输错误、无锁错误；但 24 小时 soak 未执行，
`S6_STAGING_SOAK_PASSED = NOT_RUN`，不得宣称稳定性门禁通过。

机器可读：`soak-metrics.json`。
