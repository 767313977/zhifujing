# S6.9 24 小时 Staging Soak 报告

- 状态：**`SOAK_24H_NOT_RUN`**
- 连续时长：0 小时（未启动正式计时）
- 证据：`soak_timeline.jsonl`、`metrics_summary.json`、`fault_injection_matrix.md`、`incident_log.md`

## 1. 前置条件核对

| 条件 | 状态 |
| --- | --- |
| A 浏览器残余项（E13/E15/E16/三视口） | **PASS**（`browser/`） |
| B 真实 OLD/NEW 版本回滚 | **PASS**（`staging/version_switch.json`） |
| D OS Egress N01–N24 | **NOT_VERIFIED / NOT_RUN**（无专用 VM） |
| G7 | 待回归（预计 PASS） |
| G0 保护资产一致 | 待 final（before 已采） |

D 未通过，因此按任务书**不启动** 24h Soak。

## 2. 为什么不能在当前环境完成

- 无专用、可快照、与宿主隔离的 Linux VM（固件虚拟化未启用）；
- 宿主工作期间不得自动重启，不得修改 BIOS/UEFI；
- WSL2 不可作为 OS 验收环境；
- 24h 连续运行需要可保持的 Staging 与监控，当前开发机无法保证不中断。

## 3. 已有但不计入 24h 的短窗结果

S6.4 的 3 分钟拆分 soak：5632 请求、0 个非预期 5xx、P95 29.35ms、5 项故障注入 PASS。
该结果只证明拆分形态可用，**不构成 24h 证据**，本轮不重复引用为通过。

## 4. 解除条件

1. 完成 C/D：独立 VM + N01–N24 全部 PASS；
2. 在 VM/隔离 Staging 启动 24h 计时，按分钟采集服务/资源/DB/Egress；
3. 执行 12 项故障注入并保留现场；
4. 结束后复核 integrity、C0/冻结文件、G0 final；
5. 任何中止条件触发即重新计时，不拼接片段。
