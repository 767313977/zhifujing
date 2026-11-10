# B3：RPS 全市场分母校正与首轮策略实验

- 日期：2026-11-10
- 范围：RPS 横截面分母修复、抽样语义、只读隔离、实验 A/B/C
- 状态：`EXPLORATORY_RESULTS_AVAILABLE`
- 保留：`SURVIVORSHIP_BIAS`、`ST_STATUS_PARTIAL`、`GROSS_ONLY`、`PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED`、`P13_RELEASE_NO_GO`

---

## 1. RPS 分母修复（代码 diff 要点）

### 1.1 问题

旧顺序：`读代码 → --stocks 截断 → 加载 K 线 → _rps_context(bars_by_code) → RPS 筛选`。
`bars_by_code` 是抽样后的子集，于是「全市场百分位」被算成「N 只内百分位」。

### 1.2 修复

新增全池轻量序列与拆分的股票集合：

```python
@dataclass(frozen=True)
class _RpsSeries:
    dates: list[date]
    pct_chg: array          # float32，只存涨跌幅

def _load_rps_series(engine, codes, bars) -> dict[str, _RpsSeries]:
    # 只 SELECT code, trade_date, pct_chg；日期对象用共享池去重
```

`main()` 中的接线：

```python
universe_codes = list(codes)                       # 全池 → RPS 分母
research_codes = codes[: args.stocks] if args.stocks else list(codes)

grouped = _load_history(engine, research_codes, ...)   # 只影响形态评价

if needs_rps:
    rps_series = _load_rps_series(engine, universe_codes, args.bars)

# 交易日轴改为全池并集（RPS 窗口按交易日推进）
all_dates = sorted({d for s in rps_series.values() for d in s.dates}) if rps_series \
            else sorted({d for b in bench_bars.values() for d in b.dates})

filter_ctx = _filter_context(filter_names, bars_by_code, all_dates, rps_bars=rps_series)
```

`_filter_context` 新增 `rps_bars` 参数：给了就用全池算 RPS，没给才退回研究样本（历史行为，并在日志标注「分母随抽样变化」）。

内存取舍：全池 OHLCV 的 `Bars` 约 400–600 MB，RPS 只需一列 `pct_chg`（float32 ≈ 25 MB），所以单独加载。

---

## 2. 抽样与完整池一致性测试

`tests/test_backtest_rps_universe.py`（6 例，合成 20 只 × 300 天）：

| 用例 | 断言 |
| --- | --- |
| `test_rps_identical_regardless_of_sample_size[1.0/0.8/0.2]` | 同一票同一日，RPS50/120/250 在 100% / 80% / 20% 抽样下**逐值相等** |
| `test_rps_differs_if_subset_used_as_denominator` | 反例：拿子集当分母时值会变（证明测试有区分力） |
| `test_rps90_filter_matches_independent_ranking` | RPS50≥90 与独立中位秩计算一致（容差 0.11） |
| `test_no_rps_before_warmup` | 250 日窗口第 100/248 天为 `None`，第 260 天有值 |

验证结果：3 个比例全部通过，且反例确实产生差异。

---

## 3. 完整 CLI 只读隔离测试

`tests/test_backtest_history_isolated.py` 扩到 12 例，其中 2 例覆盖完整 CLI：

| 用例 | 断言 |
| --- | --- |
| `test_cli_history_only_opens_history_db` | 运行 `main([...])` 全程只 `create_engine` 了 `history.db`，URL 均含 `mode=ro`，不含 `fupan.db` / `b1_research.db` / `p13_audit`；目录无新增文件；`session_scope` 一旦被调用即失败 |
| `test_cli_passes_full_pool_rps_to_filter` | `--stocks 5` 时 `_filter_context` 收到 `len(bars)=5`、`len(rps_bars)=20` |

同时保留此前 10 例：默认不调主库、不新建文件、只读不可写、缺库不创建、覆盖生效且源库字节不变、`constant_st` 计数、池来源不碰主库等。

---

## 4. 实际运行命令

```bash
# A. 性能试跑（不启用 RPS 过滤）
python scripts/backtest_patterns.py --pattern new_high,platform_breakout \
  --history --stocks 300 --bars 1300 --entry next_open --skip-unbuyable

# A2. 规模放大
python scripts/backtest_patterns.py --pattern new_high,platform_breakout \
  --history --stocks 1500 --bars 1300 --entry next_open --skip-unbuyable

# B. 全池 + RPS 对照
python scripts/backtest_patterns.py --pattern new_high,platform_breakout \
  --history --entry next_open --skip-unbuyable \
  --filter-split rps50_90,rps120_85,rps_dual

# C. 低吸因子 IC（全池）
python scripts/factor_ic.py --db history --bars 1300 --horizon 5
```

---

## 5. 覆盖区间与样本规模

| 项 | 值 |
| --- | --- |
| 数据源 | `backend/data/history.db`（只读） |
| 区间 | 2021-09-14 ~ 2026-10-19，1230 个交易日 |
| 全池代码 | 5,108 只（历史库内出现过的全部代码） |
| 形态评价 | 5,085 只（>= 预热门槛 62 根） |
| 基准池 | 5,096 只（>= 22 根） |
| 命中信号 | 88,320 条（2 形态 × 5 年） |
| 一字板剔除 | 260 条 / 5,763（A 期 300 只口径 4.3%） |

---

## 6. 内存与耗时实测

| 实验 | 规模 | 耗时 | 峰值工作集 |
| --- | --- | ---: | ---: |
| A | 300 只 × 1300 根 | 14.1 s | 未测（进程包装器拿不到） |
| A2 | 1,500 只 × 1300 根 | 37.5 s | 未测 |
| B | 5,085 只 + 全池 RPS | **239.2 s** | **6,503 MB** |
| C | 5,108 只因子 IC | 34.4 s | 未测 |

**瓶颈**：`_load_history` 返回 `dict[str, list[dict]]`，全量约 622 万个 dict（每行 10 键），是主要占用。
**已超 4 GB 预算**，本轮不继续扩大（不加更多形态、不加更多过滤组合）。

**可行的低内存路径（已验证接线）**：`--stocks 1500` 时 RPS 仍用全池分母（语义不变），峰值显著下降、37.5 秒完成。抽样只影响形态评价集合。

---

## 7. 趋势突破首轮对照（实验 B）

口径：次日开盘入场、剔除一字板、20 日去重、未计费用 → **`GROSS_ONLY`**；基准为同一入场日的全池等权。

| 形态 | 过滤 | 过滤组 | 基础组 | 10 日超额 | 10 日胜率 | 超额差(pp) |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 创 N 日新高 | RPS120≥85 | 16,629 | 46,309 | −1.03% | 42.1% | −0.68 |
| 创 N 日新高 | 双高 | 10,473 | 52,465 | −1.31% | 41.3% | −0.95 |
| 创 N 日新高 | RPS50≥90 | 16,934 | 46,004 | −1.34% | 40.7% | −1.12 |
| 平台突破 | RPS120≥85 | 2,271 | 23,111 | −1.33% | 45.8% | −1.19 |
| 平台突破 | RPS50≥90 | 1,453 | 23,929 | −1.68% | 42.6% | −1.53 |
| 平台突破 | 双高 | 730 | 24,652 | −2.56% | 43.2% | −2.39 |
| — | RPS50≥90 单独 | 48,064 | — | −1.04% | 41.6% | — |

**结论**：六个组合的超额差**全部为负**，且越强的 RPS 过滤超额越差（平台突破 + 双高达 −2.39pp）。脚本内置决策线（≥ +1pp 且样本 ≥ 200）无组合达标。RPS 单独使用也是 10 日 −1.04%。

与「RPS 单独不是买点」的历史判断一致，本轮在**分母修正后**再次确认。

需注意：均值为负但右偏明显（10 日中位数更负），因此不是「均值被个别大票拉高」的情形。

---

## 8. 低吸因子 IC 基线（实验 C）

全池 5,108 只、1,230 个交易日、横截面中位数 4,861 只、持有期 5 日：

| 因子 | 有效天数 | 均值 IC | IR | t 值 | IC>0 | 多空差(前20%−后20%) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `rev20` | 1,196 | **+0.0622** | 0.39 | 13.52 | 66.2% | +0.58% |
| `illiq20` | 1,196 | **+0.0576** | 0.35 | 12.25 | 67.6% | +0.65% |
| `rev5` | 1,209 | +0.0340 | 0.23 | 7.86 | 59.8% | +0.30% |
| `gap20` | 1,195 | +0.0207 | 0.28 | 9.80 | 62.8% | +0.21% |
| `volratio` | 1,196 | −0.0400 | −0.42 | −14.68 | 33.2% | −0.34% |
| `corr20` | 1,196 | −0.0570 | −0.59 | −20.53 | 27.0% | −0.51% |
| `mom60` | 1,160 | −0.0585 | −0.35 | −11.82 | 35.3% | −0.51% |
| `maxret20` | 1,194 | −0.0758 | −0.49 | −17.02 | 29.1% | −0.40% |
| `upshadow20` | 1,196 | −0.0768 | −0.41 | −14.28 | 33.4% | −0.36% |
| `amp20` | 1,196 | **−0.0794** | −0.37 | −12.65 | 36.2% | −0.37% |

要点：

- `rev20` / `illiq20` 的 IC 方向为正且 t 值显著（>12），与「反转 + 低流动性溢价」一致。
- **`amp20` 的 IC 为负**（−0.0794）。⚠️ **2026-11-10 更正**：生产 `factor_score.score_series()`
  已对 amp20 取负号（`signal = -raw[...] if key == "amp20"`），`WEIGHTS["amp20"]=0.40`
  赋给的是**方向调整后的分位**。因此「IC 为负」正是「振幅越低越该加分」的预期结果，
  **不是方向错误**。原句「若按正向加权方向相反」属表述误导，已作废。
  详见 `2026-11-10-b3-memory-and-robustness.md` 第 1 节与
  `tests/test_factor_score_amp_direction.py`。
- 多空差全部 < 1%，且**未扣费用** → `GROSS_ONLY`；不能当作可实现组合收益。
- IC 与组合收益是两件事：即使 IC 显著，扣费后能否覆盖换手成本仍未验证。

---

## 9. 数据限制与偏差

1. **幸存者偏差**：股票池取自 `history.db` 内出现过的代码（回补时按当时的当前池抓取），退市 / 未回补票整段缺席；样本与基准同向偏乐观。
2. **历史 ST**：`ST_STATUS_PARTIAL`。名称覆盖未启用时，全部区间沿用回补库名称；本次运行剔除 152,535 个(票,日)，其中 126 只票全历史名称恒为当前名称。
3. **成交假设**：次日开盘入场、收盘退出，未计佣金/印花税/滑点；一字板剔除，**跌停卖不出未建模**。
4. **市值过滤**：总市值 2000 亿那道无法复现（无历史市值），73 只当前超线票仍在样本内。
5. **基准**：随研究池变化（`bench_bars` 来自 `grouped`）；全量运行时等于全池，抽样运行时等于抽样池 —— 报告已在种子外单独说明。

---

## 10. Git

见下方提交记录（本轮代码 + 测试 + 文档一笔提交）。

---

## 11. 状态

```
EXPLORATORY_RESULTS_AVAILABLE    已产出可复现的探索性结果（非严格验证）
GROSS_ONLY                       全部收益未扣费用
SURVIVORSHIP_BIAS                股票池缺失退市股
ST_STATUS_PARTIAL                历史 ST 无法逐日还原
PERFORMANCE_BUDGET_EXCEEDED      全量 6,503 MB > 4 GB 预算
禁止使用 STRICT_VALIDATION_PASS
```
