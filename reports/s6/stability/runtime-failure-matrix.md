# S6-D3 运行稳定性与失败矩阵

| 场景 | 现状 / 证据 | 结论 |
| --- | --- | --- |
| worker 多进程配置一致性 | 启动命令无 `--workers`，uvicorn 默认 1；代码注释要求单 worker | 设计上单 worker；多 worker 未验证（N/A） |
| 多 worker Egress 统计聚合 | 单 worker，无需聚合；`metrics()` 明确写「不适用」 | N/A |
| DB connection pool | SQLAlchemy + SQLite，`check_same_thread=False`、`timeout=15` | 未做池化压测 → `NOT_VERIFIED` |
| SQLite busy/locked | `timeout=15` 写锁等待；并发压测只覆盖缓存，不覆盖写锁 | `NOT_VERIFIED` |
| 请求超时 | 外部来源有 timeout（iFinD/腾讯/东财等）；akshare 用 `_call_bounded` 限时 | 代码级 PASS，未做端到端超时演练 |
| 后台任务误启动 | 灰度 `SCHEDULER_ENABLED=false`；readiness 检查调度器实例 | PASS（应用层） |
| graceful shutdown / SIGTERM | systemd `TimeoutStopSec=30`；lifespan `finally: stop_scheduler()` | 代码级 PASS；Windows 开发机未实测信号 |
| 服务重启 | `test_startup_order_and_readiness_endpoint` 证明每次 lifespan 重新安装闸门 | PASS（应用层）；OS 规则重启未验证 |
| 内存增长 | 基准 peak 2.4–13.5 MB；未做长时间 soak | `NOT_VERIFIED`（无 soak 数据） |
| 日志轮转 | `RotatingFileHandler` 2 MB × 3 | PASS（代码级） |
| 缓存清理 | 形态缓存 10 分钟 TTL；单飞状态有界（1000 key 测试） | PASS |
| 磁盘空间不足 | 未模拟 | `NOT_VERIFIED` |
| 备份失败 | 备份任务在调度器内，灰度关闭；未模拟失败 | `NOT_VERIFIED` |
| 依赖服务不可用 | `ExternalFetchBlocked` → 503 + reason code；来源失败有重试/降级 | 代码级 PASS，未做故障注入 |

## 结论

- 与灰度直接相关的稳定性项（单 worker、调度器关闭、闸门重启后重装、日志轮转、
  单飞有界）在应用层有证据。
- 数据库锁、soak、磁盘、备份与故障注入没有真实证据，灰度审批材料中列为
  `NOT_VERIFIED`，不作为已通过项。
