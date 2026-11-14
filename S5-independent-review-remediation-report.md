# 致富经 zhifujing — S5 独立审查整改报告

- 日期：2026-11-14（Asia/Shanghai）
- 仓库：`c:\Users\76731\Documents\trae_projects\fupan`
- 基线：`692a692f8d7db39a300796f96c05e2b31d4f886a`（任务书指定的 S5 最终提交）
- 整改分支：`fix/s5-independent-review`（从上述精确 SHA 创建）
- 任务书：`zhifujing_S5_independent_review_and_remediation_task_2026-11-14.md`
- 最终状态：**`S5_REVIEW_FIXES_IMPLEMENTED_PENDING_SECOND_REVIEW`**

> 本批次是 **S5 独立审查整改批次**，不是 S6。未合并 master、未部署、未重启生产、未写生产库、
> 未执行真实腾讯 / iFinD / 东财 / 开盘红 / Tushare 采集，未修改 C0 五脚本、`forward_c0/*`、
> 冻结信号、`deploy/*` 与调度配置。

---

## 0. Gate 0：基线与隔离

| 项 | 结果 |
| --- | --- |
| 远端包含 `692a692` | 是（本地分支与 `origin/feature/stock-screening-v2` 一致） |
| 工作区 | 创建分支前 `git status --short --branch` 干净 |
| 隔离方式 | 从精确 SHA 新建本地分支 `fix/s5-independent-review`；**未建独立 worktree** |
| 未用 worktree 的原因 | 本机验收依赖仓库内固定的 `.venv` 与 `frontend/node_modules`（S5 门禁就是在这套依赖下复跑）。独立 worktree 不带这两个目录，重新安装会改变被测环境与版本，反而破坏可复现性。整改期间未带入任何未提交修改，所有改动只在本分支，原分支 `feature/stock-screening-v2` 未被改写。 |
| C0 五 blob | `1408f9e3…fdf2a`、`8064d670…fab5f`、`6ff3eb08…9abe`、`2f8979d2…2ca0`、`32b6f2ff…e80f`，与 S5-dependency-inventory 一致 |
| 冻结信号 SHA256 | `1F80D1B6…7B37`（jsonl）、`44301EB5…DD50`（meta.json），一致 |
| 三库大小 / mtime | `fupan.db` 513,519,616 B / 2026-10-31 22:26:44；`b1_research.db` 1,441,378,304 B / 2026-11-09 12:23:12；`forward_research.db` 67,055,616 B / 2026-11-11 17:17:58 |
| 三库 integrity | 全部 `ok`（`mode=ro&immutable=1` 只读打开） |

---

## 1. 整改关闭矩阵

### P1 级（4/4 已修复）

| 编号 | 结论 | 修复提交 | 关键实现 | 新增 / 改写测试 |
| --- | --- | --- | --- | --- |
| P1-01 池冲突后得泄漏 | **PASS** | `ae86f3f` | `_resolve_pool_evidence()` 先判来源可信、再判 `known_at<=cutoff`；已知区只放已证明成员；冲突要求全部构成来源可证明；盘后冲突进 `unknown_or_later.pool`；`pool_quality` 移到顶层 `source_quality`；一侧已知时输出正向观察 + `pool_scope_complete=false`，不断言无冲突 | 改写 `test_pool_conflict_survives_time_demotion`（原断言固定错误行为），新增 `test_post_cutoff_conflict_never_enters_known_area`、`test_partially_known_conflict_is_not_asserted`、`test_missing_broken_log_keeps_uncertainty`、`test_conflict_with_all_sources_known_before_cutoff_is_visible` |
| P1-02 naive 时区策略 | **PASS** | `ae86f3f` | 删除 `evaluate_known_time` 的隐式默认 true（改为必填参数）；`build()` 读显式配置 `research_naive_timestamps_trusted`（默认 **false**）；`models._now()` 改为 `now_cn()` 去 tzinfo（确定性 CST naive）；`known_at` 规范化为 aware +08:00；响应新增 `time_policy`；三桶不变量新增 `known_at`/`cutoff_at` 一致性检查 | 服务级 `test_service_default_untrusted_naive_demotes_pool`、`test_service_trusted_naive_boundaries[14:59/15:00/15:01]`、`test_service_known_at_is_aware_cst`、`test_service_theme_time_also_requires_naive_policy`；`test_s3_integration.py` 显式声明合成库时区（opt-in，不放宽断言） |
| P1-03 socket 绕过 | **PASS** | `bb0a3c9` | 新增 `connect_ex`、UDP `sendto`、平台存在时 `sendmsg`；回环只接受严格 loopback IP；`localhost` 固定映射 `127.0.0.1`（`ip6-localhost`→`::1`）；`localhost.localdomain`、空 host、`None`、普通主机名一律拦截；Unix socket 保持；`stats().enabled` 返回有效状态 | `test_direct_socket_connect_public_ipv4_is_blocked`、`test_connect_ex_public_ipv4_is_blocked`、`test_public_ipv6_connect_is_blocked`、`test_udp_sendto_public_address_is_blocked`、`test_udp_sendmsg_public_address_is_blocked`、`test_hostname_and_empty_host_do_not_bypass[...]`、`test_localhost_is_mapped_to_loopback_not_dns`、`test_ipv6_loopback_still_allowed`、`test_unix_domain_socket_is_not_blocked`、`test_dns_is_not_intercepted_but_connect_is` |
| P1-04 敏感 URL 泄漏 | **PASS** | `bb0a3c9` | requests 拦截日志只记录 method + hostname；`ExternalFetchBlocked` 只带 reason code + 安全来源标签；503 返回固定文案 + `reason_code`；`ensure_external_fetch(context)` 剔除 URL/path/query/userinfo；`danjuan` / `etf_exchange` 改传安全标签 | `test_requests_blocked_url_secrets_are_redacted_everywhere`（caplog + 异常 + HTTP body + stats 四处断言）、`test_ensure_external_fetch_context_url_is_redacted`、`test_blocked_response_is_503_and_marked_offline`、`test_stats_never_exposes_urls_or_tokens` |

### P2 级（6/7 已修复，1 项保留为未关闭性能项）

| 编号 | 结论 | 修复提交 | 关键实现 | 测试 |
| --- | --- | --- | --- | --- |
| P2-01 空集合后失败仍 VERIFIED | **PASS** | `01e79c0` | `_pool_source_status()` 先看最新尝试：`ok` 才 VERIFIED；最新 failed/skipped/partial 且有历史有效快照 → PARTIAL；完全没有成功尝试 → UNVERIFIABLE；up 的 `ok+0` 仍异常 | `test_broken_empty_snapshot_then_latest_failed_is_partial`、`test_broken_without_any_ok_is_unverifiable_not_empty_trusted` |
| P2-02 三池快照日期互相继承 | **PASS** | `01e79c0` | up/broken/down 各自维护有效快照日期；broken/down 合法空集合也算有效；新增窗口外 baseline（每来源最多 1 条 SQL）；组合视图只在三池同日有效时给日期，另给 `latest_valid_snapshot_up/broken/down` | `test_pool_latest_valid_snapshots_are_per_source` |
| P2-03 0931 PENDING 误用到历史日 | **PASS** | `01e79c0` | `post_open_pending` 改为 `day == today` 且今日为交易日、当前北京时间 < 09:31；历史日缺 0931 → MISSING；来源失败 → UNVERIFIABLE；0926/0931 的 latest attempt 与 latest valid snapshot 分开 | `test_auction_pending_is_scoped_to_today_only`、`test_auction_result_and_post_open_have_separate_attempt_and_snapshot`、`test_auction_legacy_log_without_coverage_fields_is_not_verified` |
| P2-04 共振冲突未要求双源可信 | **PASS** | `1f0ab70` | `STATE_CONFLICT` 必须 `up_trusted and broken_trusted`；任一来源失败/缺日志 → UNKNOWN；桶计数、`observed_limit_up_count`、分页行共用同一判定 | `test_conflict_requires_both_sources_trusted` |
| P2-05 D+1 关键字段 NULL 仍 VERIFIED | **PASS** | `1f0ab70` | 检查 `pct_chg`/`close`；NULL 不映射 0、不判 VERIFIED；`limit_up_again` 要求样本有效 + D+1 涨停池可信；输出 `sample_valid` / `missing_fields` | `test_d1_null_pct_chg_is_partial_not_verified`、`test_d1_null_close_is_partial_and_keeps_pct`、`test_d1_valid_fields_and_trusted_pool_is_verified`、`test_d1_quote_with_untrusted_pool_is_partial` |
| P2-06 对账器条件过宽 | **PASS** | `e988eb6` | 严格 `STATE_MATRIX`；不匹配必须有具体 reason code 才归 explained；新增负向 fixture；unexpected 非零退出；输出抽样策略 / 已抽日期 / 未覆盖日期 / 覆盖率 / 是否全历史；拒绝写模式库 | `tests/test_s5_quality_audit.py` 5 条；真实库抽样见 §3 |
| P2-07 SQL ≤25 未达标 | **未关闭（DEFERRED）** | — | 10 板块 OR：**53 SQL / 190.20 ms / 11.3 MB**；耗时达标，SQL 未达标；新增 3 条来自 P2-02 的历史有效快照查询；本轮不为达标削弱审计能力 | 保留 `PERF_SQL_BUDGET_NOT_MET`，需负责人书面批准新预算或单独立项重构 |

### 附带修复（任务书 B3/B4/C3）

| 项 | 结论 | 实现 | 测试 |
| --- | --- | --- | --- |
| B3 覆盖字段校验 | PASS | 旧帧日志缺 `requested/returned/valid/written_codes` → `FRAME_*_COVERAGE_FIELDS_MISSING`，不得 VERIFIED；`market_coverage_proof=false` | `test_auction_legacy_log_without_coverage_fields_is_not_verified` |
| B4 失败事务与日历依赖 | PASS | `_safe_source` 异常后 `rollback`；交易日历不可用 → 整页 `SOURCE_ERROR` + `CALENDAR_DEPENDENCY_UNAVAILABLE`，不伪装六来源未采集 | `test_failed_transaction_is_rolled_back_before_next_source`、`test_missing_calendar_is_structural_error_not_six_not_collected` |
| C3 工作台质量两层状态 | PASS | `quality.sources` 同时给 `source_status` / `combined_view_status`；组合视图不可信原因进顶层 `warnings`；新增 `data_as_of_note` | `test_quality_block_lists_all_sources`、`test_combined_view_warning_reaches_top_level` |

---

## 2. 提交清单

| # | 提交 | 主题 |
| --- | --- | --- |
| 1 | `ae86f3f` | `fix(pit): prevent post-cutoff pool leakage and require explicit naive tz policy`（P1-01 + P1-02 + A3） |
| 2 | `01e79c0` | `fix(quality): correct pool snapshot status, per-source dates and scoped pending`（P2-01/02/03 + B3/B4） |
| 3 | `1f0ab70` | `fix(research): require trusted conflict and valid d1 fields`（P2-04/05 + C3） |
| 4 | `bb0a3c9` | `fix(security): redact blocked URLs and close socket bypasses`（P1-03/04 + D3） |
| 5 | `e988eb6` | `test(quality): make cross-service consistency checks strict`（P2-06 + 报告） |
| 6 | 见 `git log` 第 6 条 | `test(pit): opt synthetic CST fixtures into trusted policy`（S3 集成测试显式 opt-in） |
| 7 | 见 `git log` 第 7 条 | `docs(review): record s5 independent remediation evidence`（浏览器 / 性能 / 安全证据 + 本报告） |

> 与任务书建议的 8 个原子提交相比实际为 7 个：P1-01 与 P1-02 同属 PIT 协议且改同一文件，B1/B2/B3 同属质量状态机，强行拆分会产生不可独立通过的中间提交。每个提交仍是单一主题，未混入无关改动。

---

## 3. 实测证据（本机真实执行）

### 3.1 工程门禁

| 项 | 结果 |
| --- | --- |
| `python -m compileall -q backend scripts` | 退出码 0 |
| `python -m ruff check backend scripts tests` | All checks passed（0 errors） |
| `python -m pytest -q` | **1595 passed / 4 skipped / 0 failed**，382.63 s（与 S5 基线相比新增 42 条，未删除、未跳过、未降低断言） |
| `npm test -- --maxWorkers=1` | **15 files / 151 tests passed** |
| `npm run lint` | 0 warnings / 0 errors |
| `npm run build` | 成功（tsc + vite） |
| Chrome headless | **43/43 通过，0 console error**，16 张截图（原 39 项 + 本批 4 项新场景） |

说明：整改后首次全量 `pytest` 出现 2 个失败，均在 `tests/test_s3_integration.py`：合成库 naive 时间在默认 `UNTRUSTED` 策略下被降级为 `UNKNOWN`，这是**预期的新行为**；已把该测试显式改为 `naive_timezone_trusted=True`（不放宽断言），复跑 0 failed。4 个 skip 为 S5 基线既有（httpx 未安装等），本批未新增 skip。

### 3.2 浏览器新增场景（43/43）

| 用例 | 结果 |
| --- | --- |
| `research_pool_pit_no_leak` | PASS：盘后池成员/冲突只出现在「不知道 / 观察日之后才取到」，已知区无成员 |
| `research_time_policy_visible` | PASS：页面显示「时区策略 UNTRUSTED」 |
| `workbench_quality_two_layers` | PASS：同时显示单来源 / 组合视图 + `data_as_of` 语义 |
| `egress_effective_state` | PASS：`enabled=false`、`locked_offline=true`、`dns_guard=NOT_INTERCEPTED`、`udp_blocked=0`、无 URL/token |

证据：`reports/s5_browser/browser_results.json`、`browser_run.md`、16 张截图。

### 3.3 离线闸门（隔离后端实测）

启动：`EXTERNAL_DATA_FETCH_ENABLED=false`、`SCHEDULER_ENABLED=false`、`WARMUP_PATTERN_CACHE=false`、`FUPAN_SCHEMA_MODE=verify`。

`reports/s5_security/egress_report.json`：

```text
enabled=false, locked_offline=true, mode=offline_locked,
attempts=2, blocked=2,
network_attempts=0, network_blocked=0,
os_network_attempts=0, os_network_blocked=0,
udp_attempts=0, udp_blocked=0,
installed=true, socket_guarded=true,
dns_guard=NOT_INTERCEPTED
```

- 2 次来源入口被提前阻断，socket 层 0 次真正尝试 → 离线模式 0 次真正外部网络访问（仅限已测进程内路径）；
- 503 响应只含固定文案 + `reason_code` + 安全 host，不含 URL/path/query。

### 3.4 质量对账（真实历史只读抽样）

```powershell
.venv/Scripts/python.exe scripts/check_s5_quality_consistency.py `
  --db backend/data/b1_research.db --start 2026-09-01 --end 2026-09-30 `
  --sample 15 --out reports/s5_quality/contract_matrix.json `
  --md reports/s5_quality/disagreement_report.md
```

结果（`fix/s5-independent-review` 时期，矩阵 v2 的旧运行）：15 个抽样交易日、60 项检查、
**通过 54 / 已解释 6 / 未解释 0**、1306 SQL、2.50 s，退出码 0。
输出含严格矩阵、抽样策略、已抽日期、未覆盖日期、`is_full_history=false` 的说明。
**15 日抽样不等于全历史完整验收**。

> 2026-11-14 二次审查整改后，该脚本的抽样策略与 sentiment 跨服务矩阵已收紧；最新一次
> clean HEAD 只读运行结果为 **通过 51 / 已解释 4 / 未解释 5、退出码 2**（60 项检查），
> 5 条未解释差异已逐条列在 `reports/s5_quality/disagreement_report.md`，完整 HEAD、命令与环境
> 见 `reports/s5_quality/contract_matrix.json` 与 `S5-second-review-remediation-report.md`。
> 本次未为「全绿」删除或放宽任何差异。

### 3.5 性能（`reports/s5_performance/*`）

10 板块 OR：**53 SQL / 190.20 ms / 11.3 MB**。耗时达标，SQL 未达标 → `PERF_SQL_BUDGET_NOT_MET`。

### 3.6 冻结与只读复核

C0 五 blob、冻结信号 SHA256、三库大小 / mtime / integrity 与 Gate 0 完全一致；全程只以
`mode=ro&immutable=1` 或临时库副本读取。

---

## 4. 未关闭 / 继续保留的限制（不得误报为已解决）

| 状态词 | 说明 |
| --- | --- |
| `PERF_SQL_BUDGET_NOT_MET` | 10 板块 53 SQL > 25；需负责人批准新预算或单独立项优化 |
| `OS_EGRESS_DENY_NOT_VERIFIED` | 只证明本进程 Python 网络栈 + 来源入口；子进程（`lark-cli`）、原生库、Windows 防火墙 / 容器 / 网络命名空间未实测 |
| DNS 未拦截 | `socket.getaddrinfo` 不拦，已在 stats（`dns_guard=NOT_INTERCEPTED`）与网络清单中明示 |
| `THEME_REAL_DATA_ACCEPTANCE_PENDING` | `sector_member` 仍无成员完成证明 |
| `REAL_AUCTION_DATA_NOT_VERIFIED` | 真实库 `auction_snapshot` 仍 0 行；本轮未做真实竞价采集 |
| `HISTORICAL_DATA_GAP_UNDER_REVIEW` | 真实库板块成员覆盖不全 |
| `PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED`、`P13_RELEASE_NO_GO` | 无生产发布权限，未合并 master、未部署 |

---

## 5. 边界声明

- 未开始 S6；
- 未合并 master、未部署、未重启生产服务；
- 未以写模式连接生产库、未执行 schema migration / 恢复；
- 未发起真实腾讯 / iFinD / 东财 / 开盘红 / Tushare 采集；
- 未修改 C0 五脚本、`forward_c0/*`、冻结信号、`deploy/*`、生产调度配置；
- 浏览器验收只使用 `%TEMP%\s5_e2e\s5.db` 合成库，后端只监听 `127.0.0.1:8014`；
- 临时 harness（`seed.py` / `drive.py` / `make_evidence.py`）不提交，提交的是结果、截图哈希与计数。

**最终状态只写：**

```text
S5_REVIEW_FIXES_IMPLEMENTED_PENDING_SECOND_REVIEW
```

停止，等待第二轮独立审查。
