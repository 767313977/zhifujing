# B3 结案：两项核验 + C0 前瞻跟踪工具

- 日期：2026-11-10
- 状态：`B3_HISTORICAL_RESEARCH_CLOSED`、`FORWARD_PAPER_TRACKING_CANDIDATE`、`SIGNAL_LEVEL_ONLY`
- 保留：`SURVIVORSHIP_BIAS`、`ST_STATUS_PARTIAL`、`PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED`、`P13_RELEASE_NO_GO`

---

## 1. 核验一：配对结果用的是什么人群

代码路径（`scripts/backtest_reversal.py`）：

```
_print_pairs → _paired(_daily_entry_mean(a), _daily_entry_mean(b))
_daily_entry_mean → _entry_samples → status in ("ok", "exit_limit_down")
```

- **没有缓存**：每次运行都从 `run_signals` 现场返回的 `Sample` 列表重算。
- 人群 = **已入场且有可确定退出价**（`ok` + `exit_limit_down`），不是只含 `ok`。
- 配对只取两边**共同有信号日**；缺失日直接排除，不填 0（`_paired` 用 `set(a) & set(b)`）。

数值为什么与上一轮（仅 `ok`）相同到 3 位小数：延迟退出只占已入场样本的 0.63%（151/24,019），
每个共同日的均值变化 < 0.001pp，四舍五入后看不出来。

新增确定性测试 `test_daily_mean_includes_delayed_exit`：构造 2 条 `ok` + 1 条 `exit_limit_down`，
断言日度均值按 3 条计算且不等于「仅 ok」的均值 —— 证明延迟退出确实进入了配对口径。

**结论：+0.381pp（t=3.84）、+0.306pp（t=3.31）保持不变，且口径已从「仅顺利退出」修正为「全部已入场」。**

---

## 2. 核验二：2024 年的真实超额

上一轮报告显示 `−0.00`（两位小数）。以三位小数重跑后：

| 策略 | 2021* | 2022 | 2023 | 2024 | 2025 | 2026 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A | −0.670 | +0.320 | +0.298 | +0.585 | +0.537 | **−0.088** |
| B | −0.165 | +0.025 | −0.050 | +0.232 | −0.547 | +0.197 |
| **C0** | **+0.948** | **+0.323** | **+0.386** | **−0.002** | **+0.222** | **+0.315** |
| C1 | −0.260 | +0.016 | −0.088 | +0.213 | −0.600 | +0.189 |

\* 2021 年只有 9–12 月（历史库起点 2021-09-14），不是完整自然年。

**C0 的 2024 年 = −0.002pp，是负值，不是零，更不是正数。**
正确表述：**「五年为正，2024 年 −0.002pp（接近零且为负），2021 年为不完整年度」**。
上一轮「六年全正」与「六年非负」两种说法都已作废。A 的 2026 年也确认为负（−0.088）。

---

## 3. 冻结的 C0 协议（`scripts/forward_track_c0.py::PROTOCOL`）

| 项 | 冻结值 |
| --- | --- |
| 策略 | C0 原三因子，复用生产 `factor_score.score_series()` |
| 权重 | rev20 **0.30** / illiq20 **0.30** / amp20 **0.40** |
| 窗口 | 20 个交易日；覆盖 ≥ 90%（≥18 行） |
| **候选池** | `history.db` 内 D 日有行情、且窗口内有效行 ≥18 的**全部代码**；含主板/创业板/科创板/**北交所**，**含 ST（不剔除）**；**不套用**生产 `stock_universe` 的流动性门槛与市值上限 |
| 流动性 | 20 日均额，只用于记录，不参与筛选 |
| 分位范围 | **当日全体候选池内**（池内评分，与网站某个既有选股池**不是同一个池**） |
| 并列次序 | 分数降序，同分按代码升序 |
| Top N | 20 |
| 信号时点 | D 日收盘后 |
| 入场 | D+1 开盘 |
| 退出 | 第 5 个交易日收盘（引擎交易日定义，跌停顺延） |
| 成本 | 30 / 50 / 100 bps 往返 |
| 数据源 | `backend/data/history.db`，`mode=ro` |

⚠️ **「评分公式相同」≠「策略相同」**：候选池不同（生产池带流动性/市值门槛，且是当日快照），
本协议是全历史库可用票。不得把两者混为一谈。

---

## 4. 工具用法（本地、手动）

```bash
# 列出已有快照
python scripts/forward_track_c0.py list

# 为库内最新交易日生成冻结快照（早于最新日会被拒绝）
python scripts/forward_track_c0.py snapshot

# 评价某个已冻结快照
python scripts/forward_track_c0.py evaluate --date 2026-10-19
```

- 输出目录：`backend/data/forward_c0/`（独立研究目录，已被 `.gitignore` 覆盖）
- 文件：`signals_YYYY-MM-DD.jsonl` + `.meta.json`（含 `content_sha256`）
- **同一天只允许一份**：已存在时返回码 2，拒绝覆盖
- **拒绝回溯生成**：`day < 库内最新交易日` → 直接退出（防止用未来行情伪装成当日记录）
- **拒绝数据未完成**：`day > 最新交易日` → 退出
- `evaluate` 先校验哈希，被改动过则拒绝
- 停止方式：不写任何定时任务，不运行时即停止；删除 `forward_c0/` 目录即清空

记录字段：`signal_date / created_at / strategy_version / git_commit / source_data_revision /
source_data_date / universe_definition / universe_count / stock_code / rank / score_total /
rev20 / illiq20 / amp20 / percentiles / avg_amount_20 / eligibility_reason`

---

## 5. 测试

| 批次 | 结果 |
| --- | --- |
| `tests/test_forward_track_c0.py` | **12 passed** |
| `tests/test_backtest_reversal.py` | **26 passed** |
| 后端全量 | **1077 passed / 3 skipped / 0 failed** |

覆盖：重复冻结拒绝、内容篡改拒绝、历史日期拒绝、未来日期拒绝、分位与排序确定性、
17 个字段齐全、`pending` 不计收益、成熟样本有收益、`mode=ro`、无网络/无调度器、
协议常量未变、延迟退出进入日度均值。

---

## 6. 前瞻观察节点（研究节点，不是统计显著性保证）

| 节点 | 看什么 |
| --- | --- |
| 20 个交易日 | 快照能否按时冻结、流程是否可操作、数据版本是否可追溯 |
| 60 个交易日 | 收益分布、成交状态占比（`unbuyable` / `exit_limit_down` / `no_exit`） |
| 120 个交易日 | 稳定性、与历史结果（净 30bps +0.28%、胜率 53.8%）的偏差 |

暂停条件：30bps 口径连续 20 个交易日的净收益为负，或 `no_exit` 占比明显高于历史（历史为 0）。

**不得用 20 日表现调整权重。**

---

## 7. 需要用户另行批准的事项

1. 启动实际每日跟踪（本工具需手动运行，未注册任何定时任务）。
2. 把快照写入业务库或对外接口。
3. 设计资金层（仓位、现金、换手、净值/回撤）—— 本轮仍为 `SIGNAL_LEVEL_ONLY`。
4. 生产部署或调度器接入。
