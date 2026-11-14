# 致富经 zhifujing — S5 第四轮独立审查整改报告

- 日期：2026-11-14（Asia/Shanghai）
- 仓库：`c:\Users\76731\Documents\trae_projects\fupan`
- 审查基线：`3060e82c77b91d45704ab8aa217c6f10e7bbbf86`（`fix/s5-third-review` 最终 HEAD）
- 第三轮代码 / 对账执行 HEAD：`c82fecef071df5915bc6129f23aaaf70ba7c9ba7`
- 整改分支：`fix/s5-fourth-review`（从 `3060e82` 精确创建）
- 最终代码 HEAD：`a338f7b1169b3d8ea21739310f670de402f3c72b`（本文档提交为随后一个 docs 提交）
- 任务书：`zhifujing_S5_fourth_review_remediation_task_2026-11-14.md`
- 裁定：`S5_FOURTH_REVIEW_REMEDIATION_REQUIRED`
- 最终状态：**`S5_FOURTH_REVIEW_FIXES_IMPLEMENTED_PENDING_FIFTH_REVIEW`**

> 本批次是 **S5 第四轮审查整改**，不是 S6。未合并 master、未部署、未重启生产、未写生产库、
> 未执行真实腾讯 / iFinD / 东财 / 开盘红 / Tushare 采集，未修改 C0 五脚本、`forward_c0/*`、
> 冻结信号、`deploy/*`、生产调度配置，未改写 SQL ≤25 的预算。

---

## 0. Gate 0：精确基线与隔离

| 项 | 结果 |
| --- | --- |
| 起始 HEAD | `3060e82c77b91d45704ab8aa217c6f10e7bbbf86`，父提交 `c82fecef071df5915bc6129f23aaaf70ba7c9ba7` |
| `3060e82` 文件清单 | 仅 12 个文档 / 报告 / 证据文件，无 app / scripts / tests 源码变更 |
| 隔离方式 | 从精确 SHA 新建 `fix/s5-fourth-review`；未建独立 worktree |
| C0 五 blob | `1408f9e3253f0c353023863821ad70544f0fdf2a`、`8064d6709a99b07e853c09c297bf26ad938fab5f`、`6ff3eb0861cb52580c2e20be2be778cf5bfb9abe`、`2f8979d299f2537427e3ab3c3031a2cb14d22ca0`、`32b6f2ff18373ece44f278a6e76db988385be80f` |
| 冻结信号 SHA256 | `1F80D1B6785B930FC9A922F027A323A36F5DDCF122D2EC865810F33523F07B37`、`44301EB5E00867E4E19D51C7DEA524AF0897CED0E7A5002CAC2F530E3FA2DD50` |
| 三库大小 / integrity | `fupan.db` 513,519,616 B；`b1_research.db` 1,441,378,304 B；`forward_research.db` 67,055,616 B；全部 `ok`（`mode=ro&immutable=1`） |
| 收尾复核 | C0 五 blob、冻结 SHA256、三库大小 / integrity 与 Gate 0 完全一致 |

---

## 1. 整改关闭矩阵

### 1.1 R4-P1-01 `SSLSocket.write()` 与 nested 跨 socket 豁免（A 包）— **PASS**

提交：`01c8ecf fix(security): guard SSLSocket.write and scope nested TLS checks per socket`

| 要求 | 实现 |
| --- | --- |
| `SSLSocket.write` 覆盖 | 新增 `_original_ssl_write` / `_guarded_ssl_write`；安装到 `ssl.SSLSocket.write`；离线时在原 `_sslobj.write()` 前校验 peer |
| `write` 语义 | 直接调用原方法，不做 `send()` alias；保留返回值、异常与 partial-write 语义 |
| guards 能力 | `stats().guards.ssl_write` 只有在原方法存在且 wrapper 确实安装后才为 true |
| nested 状态 | 从线程级整数深度改为**按 socket 的嵌套栈**（`active_sockets` 存 `id(sock)`） |
| 同 socket 嵌套 | 只跳过重复计数，**仍执行 `_assert_stream_peer_allowed`** |
| 跨 socket | 另一个 socket 不继承豁免，必须完整计数并校验 peer |
| 中途切离线 | `sendall` / `sendfile` 每个内部 `self.send()` 都重新判定；切换后后续块被阻断 |
| 异常安全 | `try/finally` 弹栈；异常路径测试断言 `_tls_nested_sockets() == []` |
| 卸载 | `uninstall_network_deny()` 恢复 `write/send/sendall/sendfile` 与 `socket.sendfile` |

测试（`tests/test_s5_offline_gates.py`，50 passed / 1 既有 httpx skip）：

- 真实 TLS：`test_real_tls_write_blocked_after_switch`（真实握手 + 切换 offline + 受控非回环 peer；`_original_ssl_write` 未被调用；`stream_attempts=1`、`stream_blocked=1`）
- 真实 loopback：`test_real_loopback_tls_send_sendall_write_sendfile`（send / sendall / write / sendfile 均真实传输，对端收到 `AAABBBCCC234567`；`sendfile` offset=2/count=6 返回 6，文件指针=8）
- 跨 socket：`test_tls_nested_state_does_not_leak_across_sockets`（A 的 sendfile 文件 `read()` 回调 B.send，B 被阻断；A 的栈清空）
- 中途切换：`test_tls_sendall_rechecks_each_block_after_offline_switch`、`test_tls_sendfile_rechecks_each_chunk_after_offline_switch`（第一块后切 offline，后续块被阻断）
- 线程隔离：`test_tls_nested_state_is_thread_local`
- 类方法分发与卸载：`test_wrappers_are_really_installed_on_ssl_and_sendfile`、`test_uninstall_restores_ssl_and_sendfile_methods`

### 1.2 R4-P2-01 真实 TLS 集成验证（B 包）— **PASS**

测试不再只用 `_FakeSock`：

- 本地 TLS server/client 使用 `tests/fixtures/tls/localhost.{crt,key}`（**test-only 自签名**，
  SAN=`DNS:localhost,IP:127.0.0.1`），真实 `SSLContext.wrap_socket` 握手，仅监听 `127.0.0.1`；
- 覆盖真实 `send` / `sendall` / `write` / `sendfile`、真实 `getpeername()`、真实 payload 往返、
  `sendfile` offset/count 与文件指针；
- 失败路径覆盖真实 TLS socket 在 online→offline 后被阻断，以及 nested 跨 socket / 中途切换。

证据：`reports/s5_security/tls_integration_evidence.json`（SHA256 `92F18940…83588`）
记录平台 `Windows-11-10.0.26340-SP0`、Python 3.13.13、OpenSSL 3.0.19、
证书 DER SHA256 `9ea2b1c805dfee270401ecfa2597fc118957fcc3bf8989d23b23832a8225398c`（不含私钥）、
期望/实际字节（3/3/3/6，总 15）、原方法调用次数（send 3 含内部块、write/sendall/sendfile 各 1）、
计数器（`stream_attempts=3`、`sendfile_attempts=1`、blocked=0）。

### 1.3 R4-P2-02 sentiment series row 缺失（C 包）— **PASS**

提交：`7e6e9b5 fix(quality): fail closed when sentiment series row is missing`

- `_sentiment_checks()` 的 `if not row: PASS` 已删除；
- 改为 `sentiment.series_row vs trade_calendar`：`NOT_COMPARABLE`，
  reason `SENTIMENT_SERIES_ROW_MISSING`，diagnostics 记录 `sample_date` 与 `series_rows`；
- 不计 PASS、不计 EXPLAINED；CLI 保持退出码 2；
- JSON 与 Markdown 都输出该 reason code；
- 矩阵版本升为 `quality-consistency-matrix-v4`；
- v2 / v3 历史矩阵完整保留，未被覆盖：
  `contract_matrix_v2.json`（`84C85935…D12EA`）、`contract_matrix_v3.json`（`01B5D1B1…2C144`）
  及对应 Markdown。

测试（`tests/test_s5_quality_audit.py`，30 passed）：

- `test_missing_sentiment_series_row_is_not_comparable`（`series=[]` / `series=[None]` 两组参数；
  断言 `passed=3`、`not_comparable=1`、`explained=0`、退出码 2、Markdown 含 reason）。

### 1.4 R4-P2-03 retry budget 生命周期（D 包）— **PASS**

提交：`a338f7b fix(concurrency): bound retry budget to single-flight generation`

- 删除无界全局 `_retry_attempted`；
- `_inflight` 改为 `key → _Flight(event, generation, retry_claimed)`；
- 原 leader flight 的 waiter 才能 `_claim_retry`，且同一 flight 只允许一次；
- retry flight 自身 `retry_claimed=True`，其 waiter 不能再次补算；
- retry leader 再次失败后，迟到 waiter 仍被拒绝；
- flight 结束即从 `_inflight` 移除，无全局 key map；flight 与 waiter 释放后可被 GC；
- 新独立请求注册全新 flight（generation=0），不受历史失败影响。

测试（`tests/test_p0_cache_concurrency.py`，17 passed）：

- `test_retry_budget_is_bound_to_flight_generation`
- `test_retry_state_container_is_bounded_after_many_failures`（1000 个不同 key 双重失败后
  `_inflight == {}`，且不再存在 `_retry_attempted`）
- `test_two_keys_retry_budgets_are_independent`
- `test_retry_success_writes_cache_and_clears_flight`
- 原并发用例断言未改：`build_count <= 2`。

压力复跑（`reports/s5_concurrency/stress_results_r4.json`，SHA256 `978CCD34…93DB`）：
clean 30/30 + 受控负载 30/30，`max_build_count=2`，无超时、无悬挂 flight。
结论 `STRESS_PASS_60_RUNS`，不声称形式化证明并发无竞态。

### 1.5 R4-P3-01 报告测试数量与安全能力声明（E 包）— **PASS**

- `S5-third-review-remediation-report.md` 已修正并给出三种口径：
  - 第三轮该文件从 **32 → 42 个 test function**，新增 **10 个**（不是 9 条）；
  - 其中一个用例 4 组参数 → `pytest --collect-only` 收集 **45 个 case**；
  - 实跑 **44 passed / 1 既有 httpx skip**；
- 明确第三轮 `ssl_stream=true` 不包含 `SSLSocket.write()`；R4 已补 `ssl_write` 并单独报告；
- 安全文档 `network_inventory.md` / `release_gate.md` 已列出 `ssl_write`、per-socket nested
  语义与真实 TLS 证据；`OS_EGRESS_DENY_NOT_VERIFIED`、DNS 未拦截继续保留。

---

## 2. 提交清单（`fix/s5-fourth-review`）

| # | 提交 | 主题 |
| --- | --- | --- |
| 1 | `01c8ecf` | `fix(security): guard SSLSocket.write and scope nested TLS checks per socket` |
| 2 | `a5d6f25` | `test(security): add real local TLS integration coverage`（证据工件） |
| 3 | `7e6e9b5` | `fix(quality): fail closed when sentiment series row is missing` |
| 4 | `a338f7b` | `fix(concurrency): bound retry budget to single-flight generation` |
| 5 | 本报告提交 | `docs(review): reconcile S5 fourth review evidence` |

> 说明：真实 TLS 测试函数与实现同属 `01c8ecf`；`a5d6f25` 是单独的集成证据工件，
> 避免把可执行测试与生成的 evidence JSON 混在同一提交。

---

## 3. 全量门禁与证据

### 3.1 工程门禁

| 项 | 结果 |
| --- | --- |
| `python -m compileall -q backend scripts` | 退出码 0 |
| `python -m ruff check backend scripts tests` | All checks passed（0 errors） |
| `python -m pytest -q` | **1659 passed / 4 skipped / 0 failed**，364.77 s（较第三轮 1648 新增 11 个 case；未删除、未新增 skip） |
| `npm test -- --maxWorkers=1` | **15 files / 153 tests passed** |
| `npm run lint` | 0 warnings / 0 errors |
| `npm run build` | 成功（490 ms） |
| Chrome headless | **45/45 通过，0 console error**（原 45 项全部保留；egress 场景追加 `ssl_write` 断言） |
| 冻结 / 三库 | C0 五 blob、冻结 SHA256、三库大小 / integrity 与 Gate 0 一致 |

### 3.2 安全门禁

浏览器实测 `/api/quality/egress`（`reports/s5_security/egress_report.json`，
SHA256 `E2BC2414…2EBAF`）：

```text
enabled=false, locked_offline=true,
stream_attempts=1303, stream_blocked=0,
sendfile_attempts=0, sendfile_blocked=0,
guards={requests:true, socket_connect:true, socket_stream:true,
        socket_sendmsg:false, socket_sendfile:true,
        ssl_stream:true, ssl_sendfile:true, ssl_write:true},
dns_guard=NOT_INTERCEPTED
```

- `requests` / socket 建连 / TCP send / UDP send / sendfile / TLS `send/sendall/write/sendfile`
  均有实际 wrapper；
- nested TLS 不跨 socket 共享豁免；`sendall` / `sendfile` 内部写块继续执行 peer/offline 检查；
- `socket_sendmsg` 在本机 Windows 不存在，`guards` 如实标 false；
- **仍然不宣称** OS 级零出网：子进程、原生库、Windows 防火墙 / 容器 / 网络命名空间未验证。

### 3.3 质量对账（真实库只读，clean 代码 HEAD `a338f7b`）

```powershell
.venv/Scripts/python.exe scripts/check_s5_quality_consistency.py `
  --db backend/data/b1_research.db --start 2026-09-01 --end 2026-09-30 `
  --sample 15 --out reports/s5_quality/contract_matrix.json `
  --md reports/s5_quality/disagreement_report.md
```

| 项 | 值 |
| --- | --- |
| Git | `a338f7b1169b3d8ea21739310f670de402f3c72b`，`branch=fix/s5-fourth-review`，`dirty=false` |
| 矩阵 | `quality-consistency-matrix-v4` |
| 抽样 | 15 / 21 个交易日，覆盖首尾；未覆盖 6 日（09-03、09-08、09-11、09-17、09-22、09-28） |
| 结果 | `total=76 / passed=65 / explained=4 / not_comparable=7 / unexpected=0` |
| 退出码 | **2**（7 条 `NOT_COMPARABLE` 未解决，fail closed） |
| 文件 SHA256 | `contract_matrix.json` = `D51C64527D66EC1D09EBEF0817E5CF23DD48EC0C62D3AB8C4F16BFB46C6F58C8`；`disagreement_report.md` = `E2DA30AABEF82C6C1638BE1E026FDFE9091C58FDDF3F142F35AA733C264470B0` |

7 条 `NOT_COMPARABLE` 与第三轮逐条一致，未重新分类、未改成 explained：

- 2026-09-14 → 09-15：缺 `603400`（无 D+1 日线行；无停牌/交易资格证据）
- 2026-09-21 → 09-22：缺 `002860`（同上）
- 2026-09-24 → 09-28：`OUTCOME_LIMIT_POOL_DEPENDENCY_UNRESOLVED`（D+1 up pool 不可信）
- 2026-09-29 → 09-30：D 日样本池 `UP_POOL_MISSING` + `METRIC_MISSING`
- 2026-09-30 → 10-08：同上

**15 日抽样不等于全历史验收**。

### 3.4 性能（`reports/s5_performance/benchmark.json`，代码 HEAD `a338f7b`）

| 场景 | SQL | 中位耗时 |
| --- | --- | --- |
| 10 板块 OR | **53** | 166.88 ms |
| 质量总览（30 天） | 21 | 37.99 ms |
| `sentiment_next_day` 250 天 | 255 | 348.97 ms |
| 研究上下文单股 | 14 | 14.29 ms |

**`PERF_SQL_BUDGET_NOT_MET` 继续保留**：53 SQL > 25，本轮未修改预算。

---

## 4. 未关闭 / 继续保留的限制

| 状态词 | 说明 |
| --- | --- |
| `PERF_SQL_BUDGET_NOT_MET` | 10 板块 53 SQL > 25 |
| `OS_EGRESS_DENY_NOT_VERIFIED` | 仅覆盖本进程 Python 网络栈；子进程 / 原生库 / 防火墙 / 容器 / 网络命名空间未实测 |
| DNS 未拦截 | `dns_guard=NOT_INTERCEPTED` |
| `THEME_REAL_DATA_ACCEPTANCE_PENDING` | `sector_member` 仍无成员完成证明 |
| `REAL_AUCTION_DATA_NOT_VERIFIED` | 真实库 `auction_snapshot` 仍 0 行 |
| `HISTORICAL_DATA_GAP_UNDER_REVIEW` | 真实库板块成员覆盖不全 |
| 7 条 `NOT_COMPARABLE` | 与第三轮一致，未解决；CLI 保持非零 |
| `PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED`、`P13_RELEASE_NO_GO` | 无生产发布权限，未合并 master、未部署 |

---

## 5. 证据文件位置

- 质量对账 v4：`reports/s5_quality/contract_matrix.json`、`reports/s5_quality/disagreement_report.md`
- 质量对账 v3 历史：`reports/s5_quality/contract_matrix_v3.json`、`reports/s5_quality/disagreement_report_v3.md`
- 质量对账 v2 历史：`reports/s5_quality/contract_matrix_v2.json`、`reports/s5_quality/disagreement_report_v2.md`
- TLS 集成：`reports/s5_security/tls_integration_evidence.json`
- 安全：`reports/s5_security/network_inventory.md`、`reports/s5_security/release_gate.md`、`reports/s5_security/egress_report.json`
- 并发压力：`reports/s5_concurrency/stress_results_r4.json`（以及 r3 的 `stress_results*.json`）
- 浏览器：`reports/s5_browser/browser_results.json`、`reports/s5_browser/browser_run.md`、截图
- 性能：`reports/s5_performance/benchmark.json`

---

## 6. 边界声明

- 未开始 S6；未合并 master、未部署、未重启生产服务；
- 未以写模式连接生产库，未执行 migration / restore / 覆盖；
- 未发起真实腾讯 / iFinD / 东财 / 开盘红 / Tushare 采集；
- 未修改 C0 五脚本、`forward_c0/*`、冻结信号、`deploy/*`、生产调度配置；
- 浏览器验收只使用 `%TEMP%\s5_e2e\s5.db` 合成库，后端只监听 `127.0.0.1:8014`；
- TLS 测试只使用 `tests/fixtures/tls/` 的 test-only 自签名证书，仅 loopback；
- 临时 harness / 压力脚本不提交；
- 未 force push、未 `reset --hard`、未改写既有分支历史；最终工作区 clean。

**最终状态只写：**

```text
S5_FOURTH_REVIEW_FIXES_IMPLEMENTED_PENDING_FIFTH_REVIEW
```
