# S6.9 短时 Smoke 故障矩阵

| 故障 | 结果 | 证据 |
| --- | --- | --- |
| SIGTERM | PASS | `{"exit_code": 1}` |
| SIGKILL_RESTART | PASS | `{"health": true, "user_db_integrity": "ok"}` |
| USER_DB_READONLY_READINESS | PASS | `{"http_status": 503, "reason": "USER_DB_NOT_WRITABLE"}` |
| RESEARCH_DB_MISSING_STARTUP_FAIL_CLOSED | PASS | `{"process_exited": true, "exit_code": 3, "ready_status": 0}` |
| USER_DB_LOCK_CONTENTION | PASS | `{"http_status": 500, "elapsed_ms": 16487.37, "bounded_lt_20000ms": true}` |

未覆盖（需 VM/磁盘快照）：受控磁盘降容、VM 重启后 Egress 持久性。
