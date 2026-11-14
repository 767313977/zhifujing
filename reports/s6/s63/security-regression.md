# S6.3 安全回归矩阵

| 项 | 层 | 结果 | 证据 |
| --- | --- | --- | --- |
| DNS UDP/TCP | 应用层 | PASS | `tests/test_s6_egress_boundary.py::test_E14_E15_external_dns_denied` |
| IPv4 / IPv6 | 应用层 | PASS | E11 / E12 |
| TCP / UDP | 应用层 | PASS | E04–E07 / E13 |
| HTTP/HTTPS/ALL_PROXY | 应用层 | PASS | E22 + `test_system_proxy_is_filtered_out` |
| localhost proxy | 应用层 | PASS（回环强制直连） | `_guarded_request` |
| direct socket | 应用层 | PASS | E19 |
| TLS send/sendall/sendfile/write | 应用层 | PASS | E08–E10 |
| socket sendfile | 应用层 | PASS | E07 |
| 已连接 socket send/sendall/sendmsg | 应用层 | PASS | E04–E06 |
| 子进程 | 应用层 | **无法覆盖**（E16 证明） | 需 OS 层 |
| 原生网络库 | 应用层 | **无法覆盖** | 需 OS 层 |
| 运行时配置变更 | 应用层 | PASS（offline 锁定） | `test_offline_mode_locks_for_process_lifetime` |
| 启动后策略文件被替换/删除 | 应用层 | PASS（fail closed） | E24 |
| readiness 与 health 区分 | 应用层 | PASS | E25 / `test_startup_order_and_readiness_endpoint` |
| 应用层计数与 OS 计数 | 口径 | PASS（`metrics()` 明确 UNKNOWN） | `test_metrics_unknown_before_install_and_counters_after` |
| **OS 层全部路径** | OS | **BLOCKED / NOT_RUN** | 无专用隔离 Linux VM |

## 结论

应用层第二道防护有效；OS 层第一道防护未验证，`S6_SECURITY_REGRESSION_PASSED = NOT_VERIFIED`。
