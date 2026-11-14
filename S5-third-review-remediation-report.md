# 致富经 zhifujing — S5 第三轮独立审查整改报告

- 日期：2026-11-14（Asia/Shanghai）
- 仓库：`c:\Users\76731\Documents\trae_projects\fupan`
- 审查基线：`ac64fa5a065ee3af2c7706a42ab3499c664bf34b`（`fix/s5-second-review` 最终 HEAD）
- 上一轮代码 / 对账执行 HEAD：`0c182d263274697f4d6acf2d02d9825b55703b55`
- 整改分支：`fix/s5-third-review`（从 `ac64fa5` 精确创建，未改写既有分支）
- 最终代码 HEAD：`c82fecef071df5915bc6129f23aaaf70ba7c9ba7`（本文档提交为随后一个 docs 提交）
- 任务书：`zhifujing_S5_third_review_remediation_task_2026-11-14.md`
- 裁定：`S5_THIRD_REVIEW_REMEDIATION_REQUIRED`
- 最终状态：**`S5_THIRD_REVIEW_FIXES_IMPLEMENTED_PENDING_FOURTH_REVIEW`**

> 本批次是 **S5 第三轮审查整改**，不是 S6。未合并 master、未部署、未重启生产、未写生产库、
> 未执行真实腾讯 / iFinD / 东财 / 开盘红 / Tushare 采集，未修改 C0 五脚本、`forward_c0/*`、
> 冻结信号、`deploy/*`、生产调度配置，未改写 SQL ≤25 的性能预算。

---

## 0. Gate 0：精确基线与隔离

| 项 | 结果 |
| --- | --- |
| 起始 HEAD | `ac64fa5a065ee3af2c7706a42ab3499c664bf34b`，父提交 `0c182d2` |
| `ac64fa5` 文件清单 | 仅 8 个文档 / 报告 / 证据文件（`S5-*-report.md`、`reports/s5_browser/*`、`reports/s5_performance/*`、`reports/s5_quality/*`），**无 app / scripts / tests 源码变更** |
| 隔离方式 | 从精确 SHA 新建 `fix/s5-third-review`；未建独立 worktree（本机门禁依赖仓库内 `.venv` / `node_modules`） |
| C0 五 blob | `1408f9e3253f0c353023863821ad70544f0fdf2a`、`8064d6709a99b07e853c09c297bf26ad938fab5f`、`6ff3eb0861cb52580c2e20be2be778cf5bfb9abe`、`2f8979d299f2537427e3ab3c3031a2cb14d22ca0`、`32b6f2ff18373ece44f278a6e76db988385be80f` |
| 冻结信号 SHA256 | `1F80D1B6785B930FC9A922F027A323A36F5DDCF122D2EC865810F33523F07B37`、`44301EB5E00867E4E19D51C7DEA524AF0897CED0E7A5002CAC2F530E3FA2DD50` |
| 三库大小 / mtime | `fupan.db` 513,519,616 B / 2026-10-31 22:26:44；`b1_research.db` 1,441,378,304 B / 2026-11-09 12:23:12；`forward_research.db` 67,055,616 B / 2026-11-11 17:17:58 |
| 三库 integrity | 全部 `ok`（`mode=ro&immutable=1`） |
| 收尾复核 | C0 五 blob、两份冻结 SHA256、三库大小 / mtime / integrity 与 Gate 0 完全一致 |

---

## 1. 整改关闭矩阵

### 1.1 R3-P1-01 TLS 与 sendfile 离线绕过（A 包）— **PASS**

提交：`0c597a5 fix(security): guard tls socket writes and sendfile after offline switch`

| 要求 | 实现 |
| --- | --- |
| 统一 peer 判定 | 新增 `_assert_stream_peer_allowed(sock, method, counter=...)`：Unix socket 放行；在线放行；离线时 `getpeername()` 失败 → `NETWORK_SEND_TARGET_UNKNOWN`；非严格回环 → `NETWORK_STREAM_SEND_BLOCKED` / `NETWORK_SENDFILE_BLOCKED` |
| TLS 写路径 | 新增 `ssl.SSLSocket.send` / `sendall` / `sendfile` 独立 wrapper；TLS 子类方法单独 patch，不再依赖 `socket.socket` 基类 |
| sendfile | 新增 `socket.socket.sendfile` wrapper，POSIX `os.sendfile` 与 Windows send fallback 都在进入原实现前判定；`ssl.SSLSocket.sendfile` 单独 patch |
| 切换后重新判定 | 每次发送读取有效开关并重新校验 peer，不沿用建连时的 online 状态 |
| 不重复计数 | `SSLSocket.sendall` / `sendfile` 内部回调 `self.send()` 通过线程局部深度抑制嵌套计数；`stream_attempts` 每次公开调用只 +1 |
| 统计口径 | 新增 `sendfile_attempts` / `sendfile_blocked`；`stream_*` 覆盖普通与 TLS `send` / `sendall` |
| 能力集合 | `stats().guards` 逐项列出实际安装能力：`requests` / `socket_connect` / `socket_stream` / `socket_sendmsg` / `socket_sendfile` / `ssl_stream` / `ssl_sendfile` |
| 幂等 / 卸载 | `_install_wrappers()` 幂等且线程安全；`uninstall_network_deny()` / `reset_guard_state()` 恢复全部原始引用并清空 `guards` |
| 安装失败 | 异常向上抛出，启动 fail closed；不会出现 `enabled=false` 但 `socket_guarded=true` 的虚假状态 |

新增测试（`tests/test_s5_offline_gates.py`，9 条；文件 44 passed / 1 既有 httpx skip）：

- `test_wrappers_are_really_installed_on_ssl_and_sendfile`（真实类方法分发，不只调 helper）
- `test_ssl_send_and_sendall_blocked_after_offline_switch`
- `test_ssl_send_and_sendall_fail_closed_when_peer_unknown`
- `test_socket_sendfile_blocks_before_original_implementation`
- `test_ssl_sendfile_blocked_before_original_implementation`
- `test_loopback_sendfile_works_for_plain_and_tls`
- `test_tls_sendall_does_not_double_count_nested_send`
- `test_unix_socket_sendall_and_sendfile_are_not_blocked`
- `test_uninstall_restores_ssl_and_sendfile_methods`
- `test_concurrent_offline_switch_blocks_all_tls_writes`

证据：`reports/s5_security/network_inventory.md`、`reports/s5_security/release_gate.md`、
`reports/s5_security/egress_report.json`。

**仍然保留**：`OS_EGRESS_DENY_NOT_VERIFIED`、`dns_guard=NOT_INTERCEPTED`、子进程 / 原生库 /
Windows 防火墙 / 容器 / 网络命名空间未验证。本包通过**不宣称** OS 级零出网。

### 1.2 R3-P2-01 已知冲突与 incomplete scope（B 包）— **PASS**

提交：`a725ac1 fix(pit): distinguish proven conflicts from incomplete pool scope`

`_resolve_pool_evidence()` 现在按「已知成员数量 + 未证明成员」分支：

| 状态 | unknown 语义 |
| --- | --- |
| `known_membership >= 2` | 冲突已证明；unknown 只输出 `POOL_SCOPE_INCOMPLETE_{POST_CUTOFF/NOT_PROVABLE}`，不再出现 `POOL_CONFLICT_NOT_PROVABLE` |
| `known_membership == 1` 且存在另一池未证明成员 | 潜在冲突：`POOL_CONFLICT_{suffix}`，`unconfirmed_membership` 明确列出未证明成员 |
| `known_membership == 1` 且无未证明成员 | 保持已证明的正向状态（UP/BROKEN/DOWN），只报 scope incomplete；不把已知成员说成未知 |
| 无已知成员 | 未证明成员 >1 → `POOL_CONFLICT_*`；=1 → `POOL_MEMBERSHIP_*`；=0 → `POOL_SCOPE_INCOMPLETE_*` |

- 已知 `pool_status` 不因另一个无成员来源 pending 而降级；
- `NOT_IN_POOL` 仍要求三来源全部可信且 cutoff 前已知；
- 已知冲突仍要求构成冲突的全部成员来源可信且 cutoff 前已知；
- 兼容字段 `as_known_on_day` 与 `at_cutoff_known` 的 scope / 成员语义一致；
- unknown 证据 summary 按上述分支改写，不再把已证明冲突描述成「冲突未能在截止前全部证明」。

新增 / 强化测试（`tests/test_s5_pit_times.py`，71 passed）：

- `test_proven_conflict_with_pending_source_is_scope_incomplete_only`（已知 CONFLICT + down pending：unknown 只报 scope incomplete，兼容字段一致）
- `test_known_positive_with_no_unconfirmed_member_keeps_status`（只有 up：unknown 保持 UP，只报 scope incomplete）
- `test_missing_broken_log_keeps_uncertainty`（补断言 `unconfirmed_membership == ["broken"]`）
- `test_post_cutoff_peer_without_rows_is_not_proved_empty`（补断言 unknown 保持 UP、未证明成员为空）

前端（`StockResearchDrawer.tsx`）：

- 未知区只在存在未证明成员时显示「未证明成员 …」；
- `unconfirmed_membership` 为空时显示「已知成员均已证明，仅来源完整性未闭环」，且不重复显示 `POOL_CONFLICT` 状态；
- 新增 Vitest：`proven conflict with pending source is not repeated as unprovable conflict`、`unconfirmed conflicting member is shown as potential conflict`。

### 1.3 R3-P2-02 情绪质量依赖感知矩阵（C 包）— **PASS**

提交：`a3f97f5 fix(quality): reconcile sentiment dependencies by source dimension`

对账器从「综合 `data_quality_status` vs D+1 日线」改为四个独立维度，矩阵升为
`quality-consistency-matrix-v3`：

| 检查 | 对照对象 | 规则 |
| --- | --- | --- |
| `sentiment.sample_pool(D …)` | D 日 `overview.limit_pool` 的 up / broken（必要时 D-1 up） | 样本池缺失/不可信 → `NOT_COMPARABLE`；overview 可信但情绪报池缺失 → `UNEXPECTED`；`sample=0` 不再拿去比对 D+1 日线 |
| `sentiment.d1_coverage` | D+1 `overview.stock_daily` + 代码级行情行 | 输出 `sample_codes` / `valid_codes` / `missing_codes` / `missing_row_codes` / `missing_pct_codes`；行存在但 `pct_chg=null` 且日线 VERIFIED → `UNEXPECTED`；无行且无停牌/交易资格证据 → `NOT_COMPARABLE`（不擅自解释为停牌） |
| `sentiment.limit_up_again` | D+1 `overview.limit_pool.pool_up` | 日线覆盖可 PASS；涨停池不可信时「是否再涨停」单独 `NOT_COMPARABLE`，不把综合 PARTIAL 归因为日线缺失 |
| `sentiment.metric(D)` | D 日 `overview.market_sentiment` | `METRIC_MISSING` 与 D 日情绪来源对账，不与 D+1 日线比较 |

- `_check()` 新增四分类 `PASS / EXPLAINED / UNEXPECTED / NOT_COMPARABLE`；
  `NOT_COMPARABLE` 不计 PASS，也不能用 reason code 洗成 EXPLAINED；
- CLI 退出码：存在 `UNEXPECTED` **或** `NOT_COMPARABLE` → 非零；
- 情绪服务新增**仅审计用**参数 `include_code_details`（默认 False，API 默认响应不变），
  输出 `sample_codes` / `valid_codes` / `missing_codes`；
- 旧矩阵 v2 证据完整保留，未覆盖、未伪造：
  `reports/s5_quality/contract_matrix_v2.json`（SHA256 `84C85935…D12EA`）、
  `reports/s5_quality/disagreement_report_v2.md`（SHA256 `8EBCD9BA…2F0B9`）。

新增测试（`tests/test_s5_quality_audit.py` 28 passed；`tests/test_sentiment_next_day.py` 含代码级诊断用例）：

- `test_sample_pool_missing_is_not_attributed_to_d1_daily`
- `test_sample_pool_and_d1_daily_are_reported_separately`
- `test_d1_limit_pool_unverifiable_keeps_daily_coverage_pass`
- `test_missing_sample_row_is_not_comparable_and_lists_codes`
- `test_missing_quote_without_tradability_evidence_is_not_comparable`
- `test_null_pct_chg_is_reported_as_missing_pct_codes`
- `test_null_pct_with_verified_daily_is_unexpected`
- `test_daily_source_unavailable_is_not_comparable`
- `test_metric_missing_is_checked_against_d_market_sentiment`（VERIFIED→UNEXPECTED / NOT_COLLECTED→NOT_COMPARABLE）
- `test_not_comparable_keeps_cli_fail_closed`
- `test_unverified_daily_unavailable_never_passes`
- `test_code_details_are_opt_in_audit_fields`

**5 条历史差异的代码级诊断（全部保留 lineage，不改成 explained）**：

| D 日 | D+1 | 分类 | 代码级证据 |
| --- | --- | --- | --- |
| 2026-09-14 | 2026-09-15 | `NOT_COMPARABLE` | 缺 1/44：`603400`；D+1 无 `stock_daily` 行；无 pct 值；本地无停牌/交易资格证据 → `D1_DAILY_ROW_MISSING_WITHOUT_TRADABILITY_EVIDENCE` |
| 2026-09-21 | 2026-09-22 | `NOT_COMPARABLE` | 缺 1/82：`002860`；同上 |
| 2026-09-24 | 2026-09-28 | `NOT_COMPARABLE` | `missing=0`、`valid=39`、`unverifiable=39`、`OUTCOME_LIMIT_POOL_UNVERIFIED`；D+1 up pool `NOT_COLLECTED` → `OUTCOME_LIMIT_POOL_DEPENDENCY_UNRESOLVED`；日线覆盖 PASS |
| 2026-09-29 | 2026-09-30 | `NOT_COMPARABLE` ×2 | `UP_POOL_MISSING`、`sample=0`，D 日 up pool `NOT_COLLECTED`；同时 `METRIC_MISSING`，D 日 `market_sentiment=NOT_COLLECTED` |
| 2026-09-30 | 2026-10-08 | `NOT_COMPARABLE` ×2 | 同上；10-08 确为交易日历中的 D+1 |

**真实库只读运行（clean 代码 HEAD `c82fece`）**：

```powershell
.venv/Scripts/python.exe scripts/check_s5_quality_consistency.py `
  --db backend/data/b1_research.db --start 2026-09-01 --end 2026-09-30 `
  --sample 15 --out reports/s5_quality/contract_matrix.json `
  --md reports/s5_quality/disagreement_report.md
```

| 项 | 值 |
| --- | --- |
| Git | `c82fecef071df5915bc6129f23aaaf70ba7c9ba7`，`branch=fix/s5-third-review`，`dirty=false` |
| 环境 | Python 3.13.13 / Node v24.19.0 / SQLite 3.50.4 |
| 矩阵 | `quality-consistency-matrix-v3` |
| 抽样 | 15 / 21 个交易日，覆盖首尾；未覆盖 6 日（09-03、09-08、09-11、09-17、09-22、09-28） |
| 结果 | `total=76 / passed=65 / explained=4 / not_comparable=7 / unexpected=0` |
| 退出码 | **2**（存在 `NOT_COMPARABLE`，fail closed） |
| 文件 SHA256 | `contract_matrix.json` = `01B5D1B1EEFF15538D2FDEC60103306335C6FE807474CC53C397EA866F72C144`；`disagreement_report.md` = `2E1B8A29BA0D7879AB83534EEA498A3F875F59C7723E7905CA8AC1668FB32FAB` |

与 v2 对比：v2 为 `60 / 51 / 4 / 5 / exit=2`。v3 检查数从 60 增至 76（新增样本池、日线覆盖、
再次涨停、D 日指标四个维度）；原 5 条 unexpected 中，2 条被定位到具体缺失代码、3 条被归因到
上游池依赖，全部转为 `NOT_COMPARABLE`，**没有改成 explained**；`explained` 仍为 4 条
（`SENTIMENT_SAMPLE_SUBSET_DAILY_PARTIAL`）。

**15 日抽样不等于全历史完整验收**。

### 1.4 R3-P2-03 并发用例瞬时失败（D 包）— **PASS（已定位并修复）**

提交：`c82fece fix(concurrency): bound single-flight retry after leader failure`

- 先在干净环境复测 30 次：30/30 通过，`build_count` 全为 2；
- 再在 8 个受控 CPU 负载线程下复测 30 次：**27/30 通过，3 次 `build_count=3`**；
- 根因：leader 异常退出后，被唤醒的 follower 在不同时点分别执行「补算」，
  每次补算都重新注册 leader；迟到的 waiter 可以在上一次补算失败后再次成为 leader，
  把一次失败放大成多次重复计算（测试阈值 ≤2 因此被打破）；
- 修复：`_register_or_join(..., retry=True)` 增加每键**一次性补算预算**
  `_retry_attempted`；补算成功后清理；新的一轮非 retry 请求重置预算；
- 未修改测试断言、未新增 skip、未用 sleep 或放宽阈值；
- 修复后重跑 30 次干净 + 30 次受控负载：**60/60 通过，`max_build_count=2`**，
  无超时、无永久阻塞、线程全部退出、异常只来自 `RuntimeError` 或 503。

证据：`reports/s5_concurrency/stress_results_pre_fix.json`（修复前，含 3 次 build=3）、
`reports/s5_concurrency/stress_results.json`（修复后 60/60）。结论标记
`STRESS_PASS_60_RUNS`，**不声称形式化证明并发无竞态**。

---

## 2. 提交清单（`fix/s5-third-review`）

| # | 提交 | 主题 |
| --- | --- | --- |
| 1 | `0c597a5` | `fix(security): guard tls socket writes and sendfile after offline switch` |
| 2 | `a725ac1` | `fix(pit): distinguish proven conflicts from incomplete pool scope` |
| 3 | `a3f97f5` | `fix(quality): reconcile sentiment dependencies by source dimension` |
| 4 | `c82fece` | `fix(concurrency): bound single-flight retry after leader failure` |
| 5 | 本报告提交 | `docs(review): record s5 third review evidence` |

---

## 3. 全量门禁与证据

### 3.1 工程门禁

| 项 | 结果 |
| --- | --- |
| `python -m compileall -q backend scripts` | 退出码 0 |
| `python -m ruff check backend scripts tests` | All checks passed（0 errors） |
| `python -m pytest -q` | **1648 passed / 4 skipped / 0 failed**，430.50 s（较上一轮 1627 新增 21 条，未删除、未新增 skip） |
| `npm test -- --maxWorkers=1` | **15 files / 153 tests passed** |
| `npm run lint` | 0 warnings / 0 errors |
| `npm run build` | 成功（565 ms） |
| Chrome headless | **45/45 通过，0 console error**（原 43 项全部保留 + 2 项本轮新增） |
| 冻结 / 三库 | C0 五 blob、冻结 SHA256、三库大小 / mtime / integrity 与 Gate 0 一致 |

浏览器新增场景：

| 用例 | 结果 |
| --- | --- |
| `research_pool_unconfirmed_membership` | PASS：未知区只显示「未证明成员 up」，不与已证明成员混写 |
| `egress_tls_sendfile_guards` | PASS：`guards.ssl_stream / ssl_sendfile / socket_sendfile` 均为 true，`stream_blocked=0`，`sendfile_blocked=0` |

浏览器证据：`reports/s5_browser/browser_results.json`（SHA256 `3EED0426…4D09`）、
`reports/s5_browser/browser_run.md` 与截图。临时 harness 位于 `%TEMP%\s5_e2e`，不提交。

### 3.2 安全门禁

浏览器实测（离线锁定，`reports/s5_security/egress_report.json`，
SHA256 `2B86B129…3EEC`）：

```text
enabled=false, locked_offline=true,
stream_attempts=1303, stream_blocked=0,
sendfile_attempts=0, sendfile_blocked=0,
guards={requests:true, socket_connect:true, socket_stream:true,
        socket_sendmsg:false, socket_sendfile:true,
        ssl_stream:true, ssl_sendfile:true},
dns_guard=NOT_INTERCEPTED
```

- 在线启动即安装 wrapper；online→offline 切换后普通 TCP、TLS、UDP、sendfile 均按策略拦截；
- 已建立 TLS 连接在切换后无法向非回环 peer 发送；
- loopback / Unix socket 不回归；
- `socket_sendmsg` 在本机 Windows 不存在，`guards` 如实标 false，不假装覆盖；
- **仍然不宣称** OS 级零出网：子进程、原生库、Windows 防火墙 / 容器 / 网络命名空间未验证。

### 3.3 性能（`reports/s5_performance/benchmark.json`，代码 HEAD `c82fece`）

| 场景 | SQL | 中位耗时 |
| --- | --- | --- |
| 10 板块 OR | **53** | 185.60 ms |
| 质量总览（30 天） | 21 | 47.59 ms |
| `sentiment_next_day` 250 天 | 255 | 1947.03 ms |
| 研究上下文单股 | 14 | 15.32 ms |

**`PERF_SQL_BUDGET_NOT_MET` 继续保留**：10 板块 53 SQL 仍 > 25；本轮未修改预算。

---

## 4. 未关闭 / 继续保留的限制（不得误报为已解决）

| 状态词 | 说明 |
| --- | --- |
| `PERF_SQL_BUDGET_NOT_MET` | 10 板块 53 SQL > 25 |
| `OS_EGRESS_DENY_NOT_VERIFIED` | 仅覆盖本进程 Python 网络栈；子进程 / 原生库 / 防火墙 / 容器 / 网络命名空间未实测 |
| DNS 未拦截 | `dns_guard=NOT_INTERCEPTED` |
| `THEME_REAL_DATA_ACCEPTANCE_PENDING` | `sector_member` 仍无成员完成证明 |
| `REAL_AUCTION_DATA_NOT_VERIFIED` | 真实库 `auction_snapshot` 仍 0 行；未做真实竞价采集 |
| `HISTORICAL_DATA_GAP_UNDER_REVIEW` | 真实库板块成员覆盖不全 |
| 7 条 `NOT_COMPARABLE` | 2 条缺行情代码（`603400` / `002860`，无停牌/交易资格证据）、3 条上游池依赖未解决、2 条 D 日情绪指标缺失；未关闭 |
| `PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED`、`P13_RELEASE_NO_GO` | 无生产发布权限，未合并 master、未部署 |

---

## 5. 证据文件位置

- 质量对账（v3）：`reports/s5_quality/contract_matrix.json`、`reports/s5_quality/disagreement_report.md`
- 质量对账（v2 历史，未覆盖）：`reports/s5_quality/contract_matrix_v2.json`、`reports/s5_quality/disagreement_report_v2.md`
- 并发压力：`reports/s5_concurrency/stress_results.json`、`reports/s5_concurrency/stress_results_pre_fix.json`
- 浏览器：`reports/s5_browser/browser_results.json`、`reports/s5_browser/browser_run.md`、`reports/s5_browser/*.png`
- 性能：`reports/s5_performance/benchmark.json`
- 安全：`reports/s5_security/network_inventory.md`、`reports/s5_security/release_gate.md`、`reports/s5_security/egress_report.json`

---

## 6. 边界声明

- 未开始 S6；未合并 master、未部署、未重启生产服务；
- 未以写模式连接生产库，未执行 migration / restore / 覆盖；
- 未发起真实腾讯 / iFinD / 东财 / 开盘红 / Tushare 采集；
- 未修改 C0 五脚本、`forward_c0/*`、冻结信号、`deploy/*`、生产调度配置；
- 浏览器验收只使用 `%TEMP%\s5_e2e\s5.db` 合成库，后端只监听 `127.0.0.1:8014`；
- 临时 harness（`seed.py` / `drive.py` / `make_evidence.py` / 压力脚本）不提交；
- 未 force push、未 `reset --hard`、未改写既有分支历史；最终工作区 clean。

**最终状态只写：**

```text
S5_THIRD_REVIEW_FIXES_IMPLEMENTED_PENDING_FOURTH_REVIEW
```
