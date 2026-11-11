# 致富经 B3：前瞻跟踪启动前最后验收

日期：2026-11-11
起始提交：`53867ecd72d7288d5d151313f095afa1dc57dec5`
代码提交：`ae18dcc`（fix：配对口径 + 前瞻工具双模式 + 独占发布）
文档提交：`4932a55`
收尾提交：`2f702d3`（fix：快照状态机 + 统一 replay/forward）

```
B3_HISTORICAL_RESEARCH_CLOSED
FORWARD_PAPER_TRACKING_CANDIDATE
FORWARD_TRACKING_NOT_STARTED
PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED
P13_RELEASE_NO_GO
```

本轮只做三件 P0 与一件 P1，不新增策略、不调参、不重构回测系统。

---

## 0. 结论摘要

| 项 | 结论 |
| --- | --- |
| P0-1 配对统计 | **找到真实 bug**：`_print_pairs` 被定义两次，后者覆盖前者，此前报的 +0.381 / +0.306 是 **ok 单口径**结果，151 笔延迟退出从未进入配对比较。修正后 all 口径为 **+0.3517（t=3.47）/ +0.2639（t=2.75）**，仍显著。此前的「占比 0.63% 所以变化小于 0.001pp」解释**错误，撤回**。 |
| P0-2 前瞻时间约束 | 新增 `--mode replay|forward`。forward 有 8 道闸门，当前真实库实测 **exit=3 拒绝**，不落任何文件。 |
| P0-3 前瞻/历史一致性 | 候选生成改为**共用同一段代码**；真实库 4 个历史日逐项全等（池、Top20、因子原值、分位、评分）。 |
| P1 完整性与并发 | 独占发布（`os.link`）+ 临时文件 + meta 提交标记；并发只成一个，半成品不会被识别为有效。 |

---

## 1. P0-1：延迟退出对配对统计的真实影响

### 1.1 根因：不是「共同冲击抵消」，是函数被覆盖

`scripts/backtest_reversal.py` 在同一文件里定义了两次 `_print_pairs`：

- 第一处（本意）：人群 = `_entry_samples`（ok + exit_limit_down）；
- 第二处（后加，实际生效）：人群 = `_daily_mean`（**仅 ok**）。

Python 以最后一次定义为准，因此第 11 轮加入 `_daily_entry_mean` 之后，实际运行的
一直是 **ok 单口径**，151 笔延迟退出在 A、B 两边同时被剔除。这正好落在任务书列举的
「样本筛选遗漏」一类，而不是「两侧冲击互相抵消」。

修复：删除重复定义，`_print_pairs` 同时输出 cond 与 all 两种口径；新增
`test_print_pairs_is_defined_once` 防止再次出现同名覆盖。

### 1.2 逐日核对（C0 / C1 / C2，5 日毛收益）

| 策略 | ok 笔 | 延迟笔 | cond 池化 | all 池化 | 池化差 | 日均变化（共同日） | 变动日 / 共同日 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| C0 | 23,868 | 151 | 0.6559% | 0.5843% | −0.0716% | **−0.053243%** | **90 / 1206** |
| C1 | 23,954 | 75 | 0.2629% | 0.2328% | −0.0302% | −0.023743% | 49 / 1206 |
| C2 | 24,039 | 31 | 0.3380% | 0.3238% | −0.0142% | −0.010834% | 27 / 1206 |

延迟退出样本自身的毛均值：

- C0：**−10.74%**（151 笔，占 0.63%）
- C1：−9.40%（75 笔，占 0.31%）
- C2：−10.70%（31 笔，占 0.13%）

> 任务书用四舍五入后的 +0.66% / +0.58% 推算得 −12.1%，方向正确；用未舍入值
> （0.6559% / 0.5843%）反解得 **−10.74%**。二者差在舍入，不在统计口径。

**1,206 个交易日里只有 90 天发生变化**，其余 1,116 天当日根本没有延迟退出 ——
所以「逐日均值差」不是每天都非零，也不是均匀分布。

### 1.3 C0 绝对变化最大的 10 个交易日

| 日期 | cond 日均 | all 日均 | Δ | 延迟笔 | 当日样本 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-05-12 | −2.2462% | −5.6684% | −3.4222% | 4 | 19 |
| 2026-06-03 | −2.8636% | −5.8491% | −2.9854% | 7 | 18 |
| 2026-05-19 | −1.6048% | −4.0675% | −2.4627% | 4 | 19 |
| 2026-05-15 | −1.4185% | −3.6883% | −2.2698% | 3 | 19 |
| 2026-05-13 | −4.2466% | −6.5125% | −2.2659% | 3 | 19 |
| 2024-02-21 | 5.9309% | 8.1301% | +2.1991% | 6 | 20 |
| 2024-05-27 | −4.2988% | −6.4965% | −2.1977% | 3 | 20 |
| 2026-05-14 | −2.8918% | −5.0368% | −2.1450% | 3 | 18 |
| 2024-05-23 | −4.8054% | −6.7375% | −1.9321% | 3 | 20 |
| 2024-05-22 | −5.4410% | −7.2102% | −1.7692% | 4 | 20 |

C1 / C2 的同类前十见脚本输出（`--detail`）。C1 最大单日变化是 2024-02-21 的
+2.8061%（6 笔延迟），C2 最大是 2026-05-14 的 −1.6634%。

### 1.4 重算配对差（HAC lag=4，共同 1,206 日）

| 比较 | 口径 | 均值差 | t | 95% CI |
| --- | --- | ---: | ---: | --- |
| C0 − C1 | cond | +0.3812 | 3.84 | [+0.1864, +0.5760] |
| C0 − C1 | **all** | **+0.3517** | **3.47** | **[+0.1528, +0.5506]** |
| C0 − C2 | cond | +0.3064 | 3.31 | [+0.1252, +0.4875] |
| C0 − C2 | **all** | **+0.2639** | **2.75** | **[+0.0758, +0.4520]** |
| L1_50 − L2_50 | cond | −0.0938 | −1.84 | [−0.1940, +0.0064] |
| L1_50 − L2_50 | all | −0.1126 | −2.19 | [−0.2133, −0.0120] |
| L1_25 − L2_25 | cond | −0.1642 | −1.70 | [−0.3535, +0.0251] |
| L1_25 − L2_25 | all | −0.1587 | −1.64 | [−0.3486, +0.0312] |

### 1.5 为什么此前两组统计看起来「完全没变」

因为两次都走了同一条 **ok 单口径** 代码。不是数值抵消，是**根本没有并入另一组样本**。
修复后差值确实变化了：

- C0 − C1：0.3812 → 0.3517，变化 **−0.0295**
- C0 − C2：0.3064 → 0.2639，变化 **−0.0424**

### 1.6 「日均收益变化」与「配对差变化」不是一回事

- **各策略日均收益变化**（逐日 all − cond 的等权平均）：C0 −0.053243pp、C1 −0.023743pp、
  C2 −0.010834pp。这是「同一条策略换成另一种口径」的位移。
- **配对收益差变化**：C0−C1 −0.0295 = (−0.053243) − (−0.023743)；C0−C2 −0.0424 =
  (−0.053243) − (−0.010834)。只有两侧位移不相等时配对差才动。

池化差（−0.0716%）与日均变化（−0.0532%）也不同：前者按笔加权，后者按日等权。
C0 每天 18~20 笔，深亏的延迟退出落在笔数少的日子时，对日均的影响被放大。

### 1.7 C0 是否仍符合前瞻候选

符合，但幅度要按 all 口径写：

- 毛均值 0.66% → **0.58%**，30bps 净 0.35% → **0.28%**；
- 相对 C1 的优势 **+0.3517pp（t=3.47）**、相对 C2 **+0.2639pp（t=2.75）**，
  95% 置信区间均不含 0；
- `no_exit = 0`，`unbuyable = 100`（占 0.41%），未解决样本为零。

结论不变：`FORWARD_PAPER_TRACKING_CANDIDATE` 仍然成立；但历史年化幅度、以及
「净超额仅 +0.03pp」这些限制同样不变。

---

## 2. P0-2：snapshot 的真实前瞻时间约束

### 2.1 为什么 `MAX(trade_date)` 不够

`history.db` 停在 2026-10-19，而今天是 2026-11-11。旧逻辑只要 `day == 库内最新日`
就放行 —— 于是「今天生成一份 10 月 19 日的快照」会被当成前瞻记录。这在语义上是
**历史重放**：生成时点之后的所有行情都已经在库里，任何「预测」都不成立。

### 2.2 两种模式

| 模式 | 文件名 | meta 标记 | 计入前瞻绩效 |
| --- | --- | --- | --- |
| `--mode replay` | `replay_signals_YYYY-MM-DD.*` | `REPLAY_ONLY` | 否 |
| `--mode forward` | `signals_YYYY-MM-DD.*` | `FORWARD_FROZEN` | 是 |

`--mode` 是**必填**项：不写就报错，避免默认落到哪一边都不清楚。
重放快照带 `replay_note`，评价时打印 `[REPLAY_ONLY · 不计入前瞻绩效]`；
用 `--mode forward` 去评价重放文件会被拒绝。

### 2.3 forward 的 8 道闸门

1. D 在 `trade_calendar` 里（表不存在或为空 → 直接拒绝，不拿自然日凑）；
2. 当前时间 ≥ D 日 **15:30**（收盘 + 日线落库等待）；
3. 当前时间 < 下一交易日 **09:15**（集合竞价前）；
4. 时区固定 `Asia/Shanghai`（`zoneinfo`，不看进程时区）；
5. 库内最新交易日 == D（没有当日数据就拒绝，不自动下载）；
6. D 日行数 ≥ 近 5 个交易日行数中位数 × **95%**；
7. D 日 OHLC/pct_chg 缺失率 ≤ 0.5%、成交额缺失率 ≤ 0.5%；
8. 候选池三因子缺失率 ≤ 2%。

落盘的 meta 记录 `completeness`：行数、近 5 日中位数、覆盖率、空值行数、
`factor_missing_ratio`、`verified_at`、`source_data_revision`（`data_revision` 表或
文件 mtime+size）。

### 2.4 当前实测

```
$ .venv\Scripts\python.exe scripts\forward_track_c0.py snapshot --mode forward
拒绝正式前瞻冻结 2026-11-11（forward）：
  - 该库没有交易日历（trade_calendar 表不存在或为空）：无法证明 D 是交易日与次日开盘时点

没有及时、合规的当日数据源时，保持 FORWARD_TRACKING_NOT_STARTED；
需要验证代码或策略一致性请用 --mode replay（输出标记 REPLAY_ONLY）。
exit=3
```

**没有生成任何文件**。这是预期行为：`history.db` 既没有交易日历，也没有 11 月的行情。

---

## 3. P0-3：前瞻 C0 与历史 C0 是否同一策略

### 3.1 结构上先统一，再谈验证

光靠测试比对两份实现不够 —— 任何一次单边改动都会让测试和实现一起漂移。本轮把
「当日候选池 + 三因子分 + 池内分位」抽成 `backtest_reversal._day_candidates`，
排序用 `_rank_top`，前瞻工具 **直接调用这两个函数**：

```python
# scripts/forward_track_c0.py
from scripts.backtest_reversal import MIN_POOL, _code_columns, _day_candidates, _net, _rank_top

pool = _day_candidates(matrix, pos, cols, j_of)   # 与历史回测同一段
series, liquidity, scores, percent = pool
codes = _rank_top([(code, item.score) for code, item in scores.items()], top_n)
```

### 3.2 真实库上的逐项比对（4 个历史日）

| 交易日 | 候选池 | Top20 一致 | 池一致 | 评分一致 | 分位一致 | 流动性一致 |
| --- | ---: | --- | --- | --- | --- | --- |
| 2023-05-10 | 4,667 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 2024-06-03 | 4,875 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 2025-03-05 | 4,928 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 2026-09-15 | 5,069 | ✅ | ✅ | ✅ | ✅ | ✅ |

Top5 示例（2025-03-05）：历史 `605007, 605500, 300165, 301588, 603797`；
前瞻 `605007, 605500, 300165, 301588, 603797`。逐只 `FactorScore`（含三个因子原值）
完全相同，分位差 < 1e-12。

### 3.3 十项条件逐条对照

| # | 条件 | 历史 C0 | 前瞻 C0 | 是否一致 |
| --- | --- | --- | --- | --- |
| 1 | 候选股票集合 | D 日有行情且 20 日窗口有效行 ≥ 18 | 同（共用 `_day_candidates`） | ✅ |
| 2 | ST 处理 | 不剔除（`history.db` 无 ST 字段，只按代码全集） | 同 | ✅ |
| 3 | 板块范围 | 主板/创业板/科创板/北交所全部包含 | 同 | ✅ |
| 4 | 停牌过滤 | 仅「D 日无行情记录」不进池，无额外长期停牌判定 | 同 | ✅ |
| 5 | 因子窗口 | 20 根有效行，`MIN_COVERAGE=0.9` → 至少 18 行 | 同 | ✅ |
| 6 | 成交额/市值 | 不设门槛（illiq20 只是打分项） | 同 | ✅ |
| 7 | 缺失因子 | 单因子缺失按 0 分位，不剔除整票 | 同 | ✅ |
| 8 | 分位候选池 | 当日全体候选池内 | 同 | ✅ |
| 9 | 排序并列 | 分数降序、代码升序（`_rank_top`） | 同 | ✅ |
| 10 | Top20 时点 | D 日收盘后、D+1 开盘入场 | 同 | ✅ |

**没有为「追求一致」改动历史定义**：历史侧只是把原有几行代码抽成函数，公式、
阈值、排序全部未变（`git diff` 可核对）。

---

## 4. P1：快照完整性与并发保护

### 4.1 明确区分两种能力

| 能力 | 本工具有 | 说明 |
| --- | --- | --- |
| 本地完整性检查 | ✅ | `content_sha256` 校验 JSONL 与 meta 是否自洽，可发现单文件被改或写入中断 |
| 独立时间戳证据 | ❌ | 不提供。同时替换 JSONL 与 meta 即可伪造一份自洽记录；`created_at` 是本机自报 |

工具与 meta 都写明了这句话，不夸大 SHA256 的防篡改能力。真正的时间证明需要
RFC 3161 时间戳、对象存储 WORM 或外部只追加留痕，本轮不引入外部服务。

### 4.2 并发与崩溃

- **独占发布**：`os.link(tmp, final)`，目标存在即失败；不用 `os.replace`（会无条件覆盖）。
- **提交标记**：先发布 JSONL，再发布 meta；meta 存在才算「有效快照」。
  meta 已存在而 JSONL 缺失时，回滚刚发布的 JSONL 并返回失败。
- **崩溃安全**：内容先写 `.tmp-<pid>-<rand>` 并 `fsync`，再发布；中途崩溃只会留下
  临时文件，不会被当成有效快照。
- **评价前校验**：哈希自洽 → 模式标记匹配 → 策略版本一致 → 行级 signal_date/mode/
  strategy_version 与 meta 一致 → 正式快照必须有 `completeness`。

### 4.3 已知残留

- `os.link` 在极少数不支持硬链接的文件系统上退化为 `O_EXCL` + 复制；该退化路径
  仍是独占创建，只是不再原子。复制中途失败会删掉半截目标文件。
- 崩溃留下的 `.tmp-*` 在进程内会被 `finally` 清掉；如果是断电这类硬崩溃，
  目录里会残留 `.tmp-*`。它不是 `.jsonl`/`.meta.json`，不会被 `inspect_snapshot`
  识别为快照，可人工删除。

---

## 5. 双文件发布边界（本轮补充）

### 5.1 状态机

| JSONL | meta | 状态 | snapshot 行为 | evaluate 行为 |
| --- | --- | --- | --- | --- |
| 不存在 | 不存在 | `NONE` | 过闸后创建 | 报「没有快照」 |
| 存在 | 存在 | `COMPLETE` | 校验后拒绝覆盖（exit 2） | 校验后评价 |
| 存在 | 不存在 | `INCOMPLETE_PUBLISH` | 拒绝，exit 4 | 拒绝，exit≠0 |
| 不存在 | 存在 | `INCOMPLETE_PUBLISH` | 拒绝，exit 4 | 拒绝，exit≠0 |
| 任一校验失败 | 任意 | `CORRUPTED_SNAPSHOT` | 拒绝，exit 5 | 拒绝，exit≠0 |

任何非 `COMPLETE` 状态都不进入前瞻绩效；工具**不自动删除、不自动重建**正式文件，
只打印人工检查与安全处置说明（含具体 `Remove-Item` 路径），并明确写了
「不要手工补写 meta —— 那等于把事后重建记录伪装成当日前瞻信号」。

`list` 现在同时扫描 `.jsonl` 与 `.meta.json`，只有一半的孤儿也会列出来并标注状态。

### 5.2 崩溃点实测

| # | 场景 | 结果 |
| --- | --- | --- |
| 1 | 写临时文件之前崩溃 | 目录为空（`test_crash_before_tmp_written`） |
| 2 | 临时文件写完、发布前崩溃 | 无正式文件、无残留 tmp（`test_crash_after_tmp_before_publish`） |
| 3 | JSONL 已发布、meta 未发布（真实崩溃） | `INCOMPLETE_PUBLISH`（`test_crash_after_jsonl_publish_before_meta`） |
| 4 | meta 临时文件写入中途崩溃 | 已有 JSONL 被回滚，无残留（`test_crash_during_meta_write`） |
| 5 | 两进程同时创建同一交易日 | 只有第一个成功，第二个返回 False（`test_concurrent_publish_only_one_succeeds`） |
| 6 | 已有完整快照重复执行 | exit 2，不覆盖（`test_duplicate_snapshot_is_rejected`） |
| 7 | JSONL 被改、meta 未变 | `CORRUPTED_SNAPSHOT`，不覆盖、不评价（`test_snapshot_refuses_corrupted_state`、`test_evaluate_refuses_corrupted_state`） |
| 8 | JSONL 与 meta 同时被替换 | **本地哈希判为 `COMPLETE`**（`test_forged_pair_passes_local_check`）—— 这是明确的能力边界，不是 bug |

场景 8 的测试是「断言缺陷存在」：它证明 `content_sha256` 只能做本地自洽检查，
不能证明未被篡改，也不能证明创建时间。

### 5.3 本轮修复的真实缺陷

- `_write_tmp` 写失败时会留下半截临时文件 → 现在失败即自删。
- `_publish_pair` 在「meta 临时文件写失败」时，先写好的 JSONL 临时文件会泄漏 →
  现在两个临时文件都在 `finally` 里清理。
- 旧版 `snapshot` 只检查文件是否存在，无法区分「完整」与「半套」；
  旧版 `evaluate` 在 meta 缺失时抛 `FileNotFoundError` 而非明确状态 → 现在统一走状态机。

---

## 6. 测试

```
$ .venv\Scripts\python.exe -m pytest -q
1106 passed, 3 skipped, 4 warnings in 226.44s (0:03:46)
```

上一轮基线 1,094 passed；本轮新增 12 项双文件发布边界测试。

| 验收要求 | 测试 |
| --- | --- |
| 延迟退出逐日均值核对 | `test_delay_audit_reports_only_affected_days` |
| 配对统计全精度复算 | `test_paired_diff_changes_when_caliber_changes`、`test_print_pairs_emits_both_calibers`、`test_print_pairs_is_defined_once` |
| 数据过期时拒绝正式快照 | `test_forward_rejects_stale_data` |
| 收盘前拒绝冻结 | `test_forward_rejects_before_close` |
| 数据未完成时拒绝冻结 | `test_forward_rejects_incomplete_day` |
| 历史重放不得标记为前瞻 | `test_replay_snapshot_is_labeled_replay_only`、`test_replay_cannot_be_evaluated_as_forward`、`test_replay_does_not_create_forward_file` |
| 候选池与历史 C0 一致 | `test_candidate_pool_matches_historical_c0`、`test_candidate_universe_includes_st_and_bse` |
| 同日并发创建只成功一次 | `test_concurrent_publish_only_one_succeeds` |
| 写入失败不产生有效快照 | `test_partial_write_leaves_no_valid_snapshot`、`test_crash_during_meta_write` |
| 信号与元数据完整性 | `test_tampered_snapshot_is_rejected`、`test_version_mismatch_is_rejected`、`test_row_level_mismatch_is_corrupted` |
| 未批准数据源无法启动 | `test_forward_rejects_without_trade_calendar` |
| 不触发真实交易/部署 | `test_module_has_no_network_scheduler_or_orders`、`test_engine_is_readonly` |

---

## 7. 统一 CLI

以 `--help` 实测为准，只有两个模式取值：

```
usage: forward_track_c0.py snapshot [-h] --mode {forward,replay}
                                    [--date DATE] [--top TOP] [--now NOW]
usage: forward_track_c0.py evaluate [-h] --date DATE [--mode {forward,replay}]
```

- `forward` = 真实前瞻冻结（默认拒绝过期/无当日数据）；
- `replay` = 历史重放（标记 `REPLAY_ONLY`，不计绩效）。

代码注释、文档、测试名、报告全部统一为 `forward`；`live` 只作为「历史别名」在
模块 docstring 里被点名一次，说明已废弃。

### 7.1 手动历史重放（只读、标记 REPLAY_ONLY）

```powershell
.venv\Scripts\python.exe scripts\forward_track_c0.py `
  --out backend\data\forward_c0 snapshot --mode replay --date 2026-09-15
.venv\Scripts\python.exe scripts\forward_track_c0.py `
  --out backend\data\forward_c0 evaluate --date 2026-09-15 --mode replay
.venv\Scripts\python.exe scripts\forward_track_c0.py list
```

实测（输出到临时目录）：

```
[REPLAY_ONLY（不计入前瞻绩效）] 2026-09-15：20 只候选，池 5069 只
  生成时间 2026-11-11T09:36:44+08:00（Asia/Shanghai），sha256=2a9d597cee854432…
快照 2026-09-15（20 只，哈希校验通过）  [REPLAY_ONLY · 不计入前瞻绩效]
状态：ok=20
已入场且有退出价 20 笔：毛均值 +3.58%
   30bps 净均值 +3.26%
   ...
  基准毛均值 +5.57%
```

单日样本只有 20 笔，且当日基准 +5.57% > 策略 +3.58%，**不能**当作策略有效的证据。

### 7.2 正式前瞻的启动条件（全部满足才可运行）

1. 获得**当日、合规、可自动落库**的 A 股行情源（日线 + 成交额 + 交易日历）；
2. 该源在 D 日 15:30 前完成当日全市场落库；
3. 提供 `trade_calendar` 表并在 D 日之前已铺到下一交易日；
4. 库内最新交易日 == D，且覆盖率/空值率/因子缺失率过闸；
5. 在 D 日 15:30 ～ 次日 09:15 之间手动运行一次：

```powershell
.venv\Scripts\python.exe scripts\forward_track_c0.py --db <前瞻库> snapshot --mode forward
```

6. 每个交易日重复一次（**不建自动调度**）；第 5 个交易日后 `evaluate`。

### 7.3 当前为什么不能启动

- `history.db` 最新只到 **2026-10-19**，今天是 **2026-11-11**，缺 16 个交易日；
- 该库**没有 `trade_calendar`**，无法证明哪天是交易日、次日何时开盘；
- 没有获准使用的当日行情源，工具不允许自动下载。

因此正式运行被拒绝（exit=3），状态保持 `FORWARD_TRACKING_NOT_STARTED`。

---

## 8. 独立前瞻行情数据源：推荐接入方案（本轮不执行）

原则：**新建独立的前瞻研究库**（如 `backend/data/forward_research.db`），
只读接入，不改 `history.db`、不写 `fupan.db`、不动生产调度器。

| # | 项目 | 推荐做法 |
| --- | --- | --- |
| 1 | 日线来源 | 与现生产同一供应商的**收盘日线**接口（只取已收盘确认值，不取盘中快照） |
| 2 | 成交额来源 | 同一接口的 `amount`（元），缺失时不得用 `volume×close` 估算后写入正式库 |
| 3 | 交易日历 | 同一供应商的交易日历，或交易所公布的年度休市安排；必须能回答「下一交易日」 |
| 4 | 使用权限 | 需你确认该账号/接口允许**本地留存与研究用途**，且允许每日批量拉取全市场 |
| 5 | 完成时间与时区 | 以 `Asia/Shanghai` 为准，15:30 前落库；若供应商晚于 15:30，闸门会自动拒绝当日冻结 |
| 6 | 全市场覆盖 | 必须含当日全部在市 A 股（含停牌无行情的代码不强制有记录），覆盖率 ≥ 近 5 日中位数 95% |
| 7 | 20 日预热 | 初次建库一次性回补 ≥ 40 个交易日，保证 D 日窗口有 20 根；之后每日增量 |
| 8 | ST / 北交所 | 代码前缀 `8/4/92` 的北交所与 ST 名称都要保留；协议已声明「含 ST、含北交所」 |
| 9 | 失败与修订 | 采集失败或关键字段缺失 → 当日拒绝冻结；晚到修订 → 只允许修订**尚未评价**的日期，已评价的日期不得改数 |
| 10 | 与历史库隔离 | 独立文件 + `mode=ro` 打开；`history.db` 只用于历史重放与一致性验证，两个库不互相写入 |

可以预置的参数（现在就能设计，但不启用）：

- `--db` 已存在，指向前瞻库即可；
- 建议再加 `--source <name>` 只在**你明确授权后**实现，用于标记数据来源与版本；
- 未授权前，工具不发起任何网络请求、不下载数据、不建定时任务。

---

## 9. 不变的状态

```
B3_HISTORICAL_RESEARCH_CLOSED
FORWARD_PAPER_TRACKING_CANDIDATE
FORWARD_TRACKING_NOT_STARTED
PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED
P13_RELEASE_NO_GO
```

未读取 35 个敏感数据库、未修改生产库、未创建调度任务、未部署、未下任何真实订单。
