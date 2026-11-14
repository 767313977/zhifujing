# 致富经 zhifujing — S5 第二轮独立审查整改报告

- 日期：2026-11-14（Asia/Shanghai）
- 仓库：`c:\Users\76731\Documents\trae_projects\fupan`
- 审查基线：`10c8e196ad402ed49298c0c8cdf9cef9d0f094dd`（`fix/s5-independent-review`）
- 整改分支：`fix/s5-second-review`（从上述精确 SHA 创建，未重写原分支历史）
- 最终代码 HEAD：`0c182d263274697f4d6acf2d02d9825b55703b55`（本文档提交为随后一个 docs 提交）
- S5 基线：`692a692f8d7db39a300796f96c05e2b31d4f886a`
- 任务书：`zhifujing_S5_second_review_remediation_task_2026-11-14.md`
- 裁定：`S5_SECOND_REVIEW_REMEDIATION_REQUIRED`
- 最终状态：**`S5_SECOND_REVIEW_FIXES_IMPLEMENTED_PENDING_THIRD_REVIEW`**

> 本批次是 **S5 第二轮审查整改**，不是 S6。未合并 master、未部署、未重启生产、未写生产库、
> 未执行真实腾讯 / iFinD / 东财 / 开盘红 / Tushare 采集，未修改 C0 五脚本、`forward_c0/*`、
> 冻结信号、`deploy/*` 与调度配置，未改动 SQL ≤25 的性能目标。

---

## 0. Gate 0：基线与隔离

| 项 | 结果 |
| --- | --- |
| 起始 HEAD | `10c8e196ad402ed49298c0c8cdf9cef9d0f094dd`，`git status` 干净 |
| 隔离方式 | 从精确 SHA 新建本地分支 `fix/s5-second-review`；**未建独立 worktree**（理由同上一轮：本机门禁依赖仓库内 `.venv` 与 `frontend/node_modules`） |
| C0 五 blob | `1408f9e3253f0c353023863821ad70544f0fdf2a`、`8064d6709a99b07e853c09c297bf26ad938fab5f`、`6ff3eb0861cb52580c2e20be2be778cf5bfb9abe`、`2f8979d299f2537427e3ab3c3031a2cb14d22ca0`、`32b6f2ff18373ece44f278a6e76db988385be80f`，与 `S5-dependency-inventory.md` 逐字一致 |
| 冻结信号 SHA256 | `1F80D1B6785B930FC9A922F027A323A36F5DDCF122D2EC865810F33523F07B37`（jsonl）、`44301EB5E00867E4E19D51C7DEA524AF0897CED0E7A5002CAC2F530E3FA2DD50`（meta.json），一致 |
| 三库大小 / mtime | `fupan.db` 513,519,616 B / 2026-10-31 22:26:44；`b1_research.db` 1,441,378,304 B / 2026-11-09 12:23:12；`forward_research.db` 67,055,616 B / 2026-11-11 17:17:58 |
| 三库 integrity | `fupan.db` / `b1_research.db` / `forward_research.db` 全部 `ok`（`mode=ro&immutable=1` 只读打开） |
| 收尾复核 | C0 五 blob、两份冻结 SHA256、三库大小 / mtime / integrity 与 Gate 0 完全一致（见 §3.1） |

---

## 1. 整改关闭矩阵

### 1.1 R2-P1-01 池来源完整性（A 包）— **PASS**

提交：`6cb901b fix(pit): require all pool sources for scope completeness`

| 要求 | 实现 |
| --- | --- |
| `required_sources` 不得由成员行推导 | 固定为 `list(POOL_TYPES)`（up/broken/down 三池） |
| 分离 observed / required / known / pending | `_resolve_pool_evidence()` 分别计算 `observed_member_types`、`required_sources`、`known_sources`、`pending_sources` |
| 单边正向成员可保留 | 已证明的 up 仍可输出 `UP`，但 `pool_scope_complete=false`，其余来源全部进 `pending_sources` |
| 任一来源未证明不得推断为空 | `NOT_IN_POOL` 只在三来源 trusted 且 `known_at <= cutoff` 时成立 |
| 冲突要求全部构成来源已证明 | `POOL_CONFLICT` 仅在冲突来源均可证明时成立，否则进 unknown |
| 后采成员不得进已知区 | 已知区 = `observed_member_types ∩ known_sources` |
| unknown 桶不误伤已证明成员 | 新增 `unconfirmed_membership` / `pending_sources`；`observed_pool_membership` 仅保留审计用途 |
| 前端 | `StockResearchDrawer` 未知区展示未证明成员 / 未证明来源 / reason_code，已知区不再暗示排他 |
| `as_known_on_day` 与 `at_cutoff_known` 一致 | 共用同一 `_resolve_pool_evidence` 结果 |

新增测试（`tests/test_s5_pit_times.py`）：

- `test_single_up_member_still_requires_peer_sources`：只有 up 成员、peer 无日志 → 已知区可显示 UP，但 scope incomplete 且 broken/down 均进 pending；
- `test_post_cutoff_peer_without_rows_is_not_proved_empty`：broken 22:00 才采集且无成员 → 不得认为已证明为空；
- `test_all_three_sources_known_with_only_up_member_is_scope_complete`：三源可信且仅 up 成员 → scope complete、UP、无冲突；
- `test_all_three_sources_known_without_members_is_not_in_pool`：三源可信且无成员 → `NOT_IN_POOL`。

### 1.2 R2-P2-01 共振排他分类（B 包）— **PASS**

提交：`10188c9 fix(research): keep resonance states unknown when peer source is untrusted`

- `state_of()` 先检查 up / broken 两个来源是否**同时可信**；
- 任一来源不可信 → 排他状态（FIRST / SECOND / THIRD_PLUS / BROKEN / CONFLICT）一律 `UNKNOWN`；
- 双源可信且都有成员 → `CONFLICT`；双源可信、仅单边成员 → 按既有规则分类；
- 桶计数、`observed_limit_up_count`、分页行共用同一判定，避免「行显示冲突、计数不算」的不一致。

新增测试（`tests/test_sector_resonance.py`）：

- `test_single_up_member_with_untrusted_broken_is_unknown`
- `test_single_broken_member_with_untrusted_up_is_unknown`
- `test_both_sources_trusted_classifies_single_side_members`

### 1.3 R2-P1-02 离线闸门运行时切换与已连接 socket（C 包）— **PASS**

提交：`46761a4 fix(security): enforce offline transition and connected socket send guards`

| 要求 | 实现 |
| --- | --- |
| online 启动后切换 offline 仍然生效 | `install_network_deny()` 启动时**总是安装**轻量 wrapper（在线透明放行）；`enabled()` 首次 online→offline 还会幂等补装 |
| 不得出现 `enabled=false` 但 `socket_guarded=false` | 安装与状态更新在同一 `_lock` 语义下完成；安装异常向上抛出，启动拒绝继续（fail closed） |
| 已连接 socket 的 send / sendall / sendmsg | 按显式地址或 `getpeername()` 判定；非回环阻断；无地址且无 peer 时 fail closed |
| `sendmsg(..., address=None)` 不再放行 | 已连接 UDP / TCP 均按 peer 判定 |
| loopback / localhost / Unix socket 继续可用 | 严格回环 IP 白名单；Unix domain socket 不拦 |
| 统计分项 | 新增 `stream_attempts` / `stream_blocked`，保留 `udp_attempts` / `udp_blocked`、`network_attempts` / `network_blocked` |
| DNS 不宣称已验证 | `dns_guard=NOT_INTERCEPTED` 保持不变 |
| offline→online | 仍需重启，保持锁定语义 |

新增测试（`tests/test_s5_offline_gates.py`，9 条）：

- `test_online_start_installs_wrapper_and_runtime_switch_enforces`
- `test_connected_send_and_sendall_blocked_after_switch`
- `test_sendmsg_with_no_address_on_connected_socket_is_blocked`
- `test_connected_udp_sendmsg_without_destination_is_blocked`
- `test_loopback_stream_send_sendall_sendmsg_allowed`
- `test_unix_socket_stream_send_is_not_blocked`
- `test_offline_to_online_still_requires_restart`
- `test_install_failure_is_fail_closed`
- `test_concurrent_switch_has_no_allow_window`

测试文件结果：42 passed / 1 既有 httpx skip（未新增 skip）。
证据：`reports/s5_security/network_inventory.md`、`reports/s5_security/release_gate.md`。

### 1.4 R2-P2-02 对账 sentiment 自比较（D 包）— **PASS**

提交：`352b1c6 fix(quality): compare sentiment status against daily source`

- 矩阵左侧改为 D+1 `stock_daily` overview 状态（`_daily_status(session, d1)`），右侧为 `sentiment_next_day.data_quality_status`；
- 明确允许组合：`VERIFIED_SOURCE→VERIFIED/EMPTY`、`PARTIAL→PARTIAL`、`UNVERIFIABLE→UNVERIFIED`、`NOT_COLLECTED→PENDING`、`SOURCE_ERROR→∅`；
- 唯一「已解释」差异：`daily=PARTIAL` + `sentiment=VERIFIED` + `missing_count=0` + 有效样本 >0 → `SENTIMENT_SAMPLE_SUBSET_DAILY_PARTIAL`；missing>0 或无有效样本一律 unexpected；
- 无 D+1 交易日时 N/A（不可比），不再与来源错误混淆；
- `_theme_checks()`：`sector_member` 为 `SOURCE_ERROR` 等非「查询成功且当日无快照」状态时，缺 `sector_code` 不再返回 N/A pass，而是 unexpected。

新增测试（`tests/test_s5_quality_audit.py`）：

- `test_sentiment_daily_mismatch_is_unexpected_and_exits_nonzero`（5 组参数：VERIFIED+PARTIAL、PARTIAL+UNVERIFIED、SOURCE_ERROR+VERIFIED、NOT_COLLECTED+VERIFIED、PARTIAL+VERIFIED+missing>0，均 unexpected 且 CLI 退出 2）；
- `test_sentiment_subset_complete_is_explained_with_reason_code`（missing=0 → explained，退出 0）；
- `test_theme_source_error_is_not_na`（SOURCE_ERROR + 无成员 → unexpected，退出 2）。

### 1.5 R2-P2-03 抽样未覆盖全区间（E 包）— **PASS**

提交：`17388b4 fix(quality): sample across full date interval`

- `sample >= len(days)` → 全量；
- `sample == 1` → 区间首个交易日；
- 其余按 `(n-1)/(sample-1)` 均匀取索引：唯一、递增、`sample>=2` 时覆盖首尾；
- `selected` 与 `uncovered` 交集为空、并集等于全部交易日；`coverage_ratio` / `is_full_history` 由实际选择结果计算；
- 输出新增 `已抽日期` 明细，`--sample` 仍限制 1~255。

新增 / 更新测试（`tests/test_s5_quality_audit.py`）：

- `test_sampling_covers_full_interval`（21/15、29/15、21/10、15/15、15/20、1/1 六组参数，检查数量、唯一递增、首尾覆盖）；
- `test_sampling_selection_and_uncovered_are_disjoint_and_complete`（21 日抽 15 日：并集守恒、交集为空、首尾覆盖）；
- `test_invalid_sample_is_rejected`（`--sample=0` / `256` 均拒绝）；
- `test_baseline_audit_has_no_unexpected` 更新为校验新抽样策略文案。

### 1.6 R2-P2-04 证据数字与真实树一致（F 包）— **PASS**

- `S5-independent-review-remediation-report.md` §3.4 的 `0 unexpected / 0 explained` 已更正为上一轮真实运行值：`total_checks=60 / passed=54 / explained=6 / unexpected=0`；并补充本轮收紧后的最新运行结果；
- 未删除任何差异、未放宽断言以追求全绿；
- 真实库只读抽样已从 **clean HEAD** 重新生成，完整记录见 §3.3。

附加提交：`0c182d2 fix(quality): record environment and exit code in audit evidence`（把 HEAD / 分支 / dirty、命令、Python/Node/SQLite 版本、退出码写入 JSON 与 Markdown 证据）。

---

## 2. 提交清单（`fix/s5-second-review`）

| # | 提交 | 主题 |
| --- | --- | --- |
| 1 | `6cb901b` | `fix(pit): require all pool sources for scope completeness`（A / R2-P1-01） |
| 2 | `10188c9` | `fix(research): keep resonance states unknown when peer source is untrusted`（B / R2-P2-01） |
| 3 | `46761a4` | `fix(security): enforce offline transition and connected socket send guards`（C / R2-P1-02） |
| 4 | `352b1c6` | `fix(quality): compare sentiment status against daily source`（D / R2-P2-02） |
| 5 | `17388b4` | `fix(quality): sample across full date interval`（E / R2-P2-03） |
| 6 | `0c182d2` | `fix(quality): record environment and exit code in audit evidence`（F 证据元数据） |
| 7 | 本报告提交 | `docs(review): record s5 second remediation evidence`（F / 报告 + 证据文件） |

> 与任务书建议的 6 个提交相比多 1 个：F 要求把环境 / 命令 / 退出码写进证据文件，这是脚本改动，
> 单独成提交而不是混入 docs，保证「先提交全部代码、再从 clean HEAD 跑真实库」可复现。

---

## 3. 实测证据（本机真实执行）

### 3.1 全量门禁

| 项 | 结果 |
| --- | --- |
| `python -m compileall -q backend scripts` | 退出码 0 |
| `python -m ruff check backend scripts tests` | All checks passed（0 errors） |
| `python -m pytest -q` | **1627 passed / 4 skipped / 0 failed**，368.39 s（较上一轮 1595 新增 32 条，未删除、未新增 skip、未降低断言） |
| `npm test -- --maxWorkers=1` | **15 files / 151 tests passed** |
| `npm run lint` | 0 warnings / 0 errors |
| `npm run build` | 成功（tsc + vite，505 ms） |
| Chrome headless | **43/43 通过，0 console error**；截图哈希见 `reports/s5_browser/browser_results.json` |
| 冻结文件 / 三库 | C0 五 blob、两份冻结 SHA256、三库大小 / mtime / integrity 与 Gate 0 完全一致 |

说明：首次全量 pytest 出现 1 条并发时序失败
（`tests/test_p0_cache_concurrency.py::test_leader_exception_all_waiters_wake`，
`build 次数应 ≤2，实际 3`），单测连续 3 次通过；在停止浏览器 / 后端负载后从干净环境复跑全量，
**0 failed**。该用例未做任何修改或跳过。

浏览器验收环境：临时 SQLite `%TEMP%\s5_e2e\s5.db`、临时账号 `s5a` / `s5b`、后端仅监听
`127.0.0.1:8014`，四个开关均为离线 / 关闭状态。本批新增 / 校验场景：

| 用例 | 结果 |
| --- | --- |
| `research_pool_pit_no_leak` | PASS：已知区无池成员；未证明来源与 `POOL_MEMBERSHIP_*` 只出现在「不知道 / 观察日之后才取到」 |
| `research_time_policy_visible` | PASS：显式展示 `naive_timezone_policy=UNTRUSTED` |
| `workbench_quality_two_layers` | PASS：单来源 / 组合视图两层状态与 `data_as_of` 语义 |
| `egress_effective_state` | PASS：`enabled=False`、`locked_offline=True`、`dns_guard=NOT_INTERCEPTED`、`udp_blocked=0` |
| `offline_sync_blocked_before_network` | PASS：503 且 `network_attempts=0`、`blocked>=1` |
| `console_errors` | PASS：0 errors |

> 浏览器 harness 的一处断言文案按真实 UI 校正为「以下池来源未能在 15:00 前证明」+ `POOL_MEMBERSHIP`；
> harness 位于 `%TEMP%`，不提交，产品代码未改。

### 3.2 性能（`reports/s5_performance/benchmark.json`，代码 HEAD `0c182d2`）

| 场景 | SQL | 中位耗时 | 峰值内存 |
| --- | --- | --- | --- |
| 10 板块 OR | **53** | 155.27 ms | 11.3 MB |
| 质量总览（30 天） | 21 | 37.56 ms | 57.4 KB |
| `sentiment_next_day` 250 天 | 255 | 334.32 ms | 13.2 MB |
| 研究上下文单股 | 14 | 11.93 ms | 2.2 MB |

**`PERF_SQL_BUDGET_NOT_MET` 继续保留**：10 板块 53 SQL 仍 > 25。

### 3.3 质量对账（真实库只读、clean HEAD）

命令（与 JSON 内 `command` 字段一致）：

```powershell
.venv/Scripts/python.exe scripts/check_s5_quality_consistency.py `
  --db backend/data/b1_research.db --start 2026-09-01 --end 2026-09-30 `
  --sample 15 --out reports/s5_quality/contract_matrix.json `
  --md reports/s5_quality/disagreement_report.md
```

| 项 | 值 |
| --- | --- |
| 完整代码 HEAD | `0c182d263274697f4d6acf2d02d9825b55703b55` |
| 工作区 | clean（`dirty=false`，于写文件前采集） |
| 环境 | Python 3.13.13 / Node v24.19.0 / SQLite 3.50.4 |
| 数据库与模式 | `b1_research.db`，`mode=ro&immutable=1` |
| 区间 / 交易日 | 2026-09-01 ~ 2026-09-30，共 21 个交易日 |
| 抽样 | 15 日（等距、覆盖首尾），`coverage_ratio=0.7143`，`is_full_history=false` |
| 未覆盖日期 | 2026-09-03、09-08、09-11、09-17、09-22、09-28 |
| 检查结果 | `total=60 / passed=51 / explained=4 / unexpected=5` |
| 退出码 | **2**（存在未解释差异） |
| 文件 SHA256 | `contract_matrix.json` = `84C85935CD8FE00C201EE5C2C498F69BFBC495E4827442E0F0EC4D4FFB3D12EA`；`disagreement_report.md` = `8EBCD9BA328CC848D2CC0EE3ACEBD361EA269BBDB528EC18C21B5D3BC722F0B9` |

**5 条未解释差异（原样保留，未放宽、未删除）**，全部是 `overview.stock_daily vs sentiment-next-day`：

| 抽样日 | D+1 日线 | 情绪次日 | 观测 |
| --- | --- | --- | --- |
| 2026-09-14 | `VERIFIED_SOURCE` | `PARTIAL`（missing=1/44） | 抽样内 1 只股票在 D+1 无行情，与全市场日线「已验证」矛盾 |
| 2026-09-21 | `VERIFIED_SOURCE` | `PARTIAL`（missing=1/82） | 同上，1/82 缺行情 |
| 2026-09-24 | `VERIFIED_SOURCE` | `PARTIAL`（missing=0，`unverifiable_count=39`，`OUTCOME_LIMIT_POOL_UNVERIFIED`） | D+1 涨停池不可信，情绪侧无法判定「是否再涨停」 |
| 2026-09-29 | `VERIFIED_SOURCE` | `UNVERIFIED`（`UP_POOL_MISSING`） | 情绪侧无样本，原因是涨停池缺失 |
| 2026-09-30 | `VERIFIED_SOURCE` | `UNVERIFIED`（`UP_POOL_MISSING`） | 同上；D+1 为 10-08 |

这 5 条是收紧后的矩阵**新暴露**的跨服务差异：前两条指向 `stock_daily` 全市场「已验证」口径与
情绪抽样缺行之间的口径不一致；后三条的驱动因素是涨停池而非日线。本轮按要求只做对账器收紧，
**未修改任一业务服务、未增加放行 reason_code、未删除差异**；是否调整日线验证口径或把池原因单列，
留给第三轮审查裁定。

**15 日抽样不等于全历史验收**；区间内另有 6 个交易日未参与本次对账。

### 3.4 安全证据

浏览器实测（离线锁定）：`network_attempts=0`、`blocked>=1`（来源入口先阻断）、
`udp_attempts=0`、`stream_attempts=0`、`dns_guard=NOT_INTERCEPTED`。
单元测试覆盖：在线启动后切换离线、`connect` / `connect_ex`、已连接 `send` / `sendall` /
`sendmsg`、UDP 无 destination、loopback、Unix socket、并发切换、安装失败 fail closed。
**OS 级零出网仍未验证**（见 §4）。

---

## 4. 未关闭 / 继续保留的限制（不得误报为已解决）

| 状态词 | 说明 |
| --- | --- |
| `PERF_SQL_BUDGET_NOT_MET` | 10 板块 53 SQL > 25；需负责人批准新预算或单独立项优化 |
| `OS_EGRESS_DENY_NOT_VERIFIED` | 只证明本进程 Python 网络栈 + 来源入口；子进程（`lark-cli`）、原生库、Windows 防火墙 / 容器 / 网络命名空间未实测 |
| DNS 未拦截 | `socket.getaddrinfo` 不拦，`dns_guard=NOT_INTERCEPTED` |
| 子进程 / 原生库 / 防火墙未验证 | 同上，不含任何 OS 级出网放行结论 |
| `THEME_REAL_DATA_ACCEPTANCE_PENDING` | `sector_member` 仍无成员完成证明 |
| `REAL_AUCTION_DATA_NOT_VERIFIED` | 真实库 `auction_snapshot` 仍 0 行；本轮未做真实竞价采集 |
| `HISTORICAL_DATA_GAP_UNDER_REVIEW` | 真实库板块成员覆盖不全 |
| 真实库对账 5 条 unexpected | 见 §3.3；未解释、未关闭，等待第三轮审查裁定 |
| `PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED`、`P13_RELEASE_NO_GO` | 无生产发布权限，未合并 master、未部署 |

---

## 5. 边界声明

- 未开始 S6；
- 未合并 master、未部署、未重启生产服务；
- 未以写模式连接生产库，未执行 migration / restore / 覆盖；
- 未发起真实腾讯 / iFinD / 东财 / 开盘红 / Tushare 采集；
- 未修改 C0 五脚本、`forward_c0/*`、冻结信号、`deploy/*`、生产调度配置；
- 浏览器验收只使用 `%TEMP%\s5_e2e\s5.db` 合成库，后端只监听 `127.0.0.1:8014`；
- 临时 harness（`seed.py` / `drive.py` / `make_evidence.py`）不提交，提交的是结果、截图哈希与计数；
- 未 force push、未 `reset --hard`、未改写 `fix/s5-independent-review` 历史。

**最终状态只写：**

```text
S5_SECOND_REVIEW_FIXES_IMPLEMENTED_PENDING_THIRD_REVIEW
```
