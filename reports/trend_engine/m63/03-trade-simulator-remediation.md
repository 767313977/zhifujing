# M6.3 Batch 2：交易模拟器整改

- 代码：`backend/app/services/trend_engine/trade_sim.py`、新增 `pricing.py`；`scripts/m63_trade_sim_remediation.py`
- 证据：`trade_sim_remediation.json`、`trade_ledger_reconciliation.csv`、`portfolio_daily_reconciliation.csv`、`corporate_action_ledger_reconciliation.csv`
- 结论：**F3/F4/F6/F7 已修复，账本不变量全部通过；但信号价域仍为 `RAW_APPROX`（无复权因子），所有交易级结果继续 `PENDING_REVIEW`。**

---

## 1. 双价格域落地

| 项 | 实现 |
| --- | --- |
| 类型 | `pricing.PriceBar` / `SignalBar` / `ExecutionBar`，字段不共用模糊 `close` |
| 信号域 | 只用 `adjusted_*`；`to_signal_bar` 在因子缺失/非法时抛 `PriceDomainError` |
| 执行域 | 只用 `raw_*`；成交、涨跌停、现金、市值全部走 `ExecutionBar` / raw series |
| 模拟器 | `SimConfig.signal_price_mode` 必须显式声明：`INVALID`（默认，拒绝）/ `RAW_APPROX` / `ADJUSTED` |
| `ADJUSTED` | 必须提供 `signal_bars`，且代码集合与执行域一致 |
| 结果标记 | `metrics.price_domain_quality`、`metrics.status`；`RAW_APPROX` → `PENDING_REVIEW` |

生产数据上 `build_price_bar` 只能得到 `adjustment_status=UNKNOWN`（无因子），因此**当前所有回测只能显式选择 `RAW_APPROX`**，不存在「静默退化」。

---

## 2. 缺陷修复

| 编号 | 问题 | 修复 | 测试 |
| --- | --- | --- | --- |
| F3 | 超过 `max_exit_delay` 后移除卖出请求，持仓仍按最后价当作可正常变现 | 引入 `Position.status`：`OPEN / EXIT_PENDING / SUSPENDED / UNVALUABLE`；超时后标记 `UNVALUABLE`、写入 `result.unvaluable` 与 `warnings`，`metrics.status=PENDING_REVIEW` | `test_unvaluable_position_is_flagged_not_silently_closed` |
| F4 | 买入用「当日收盘权益」定仓位，属日内前视 | 改用 `previous_equity`（上一交易日收盘权益），即开盘时已知信息 | `test_sizing_uses_previous_close_equity_not_today_close`（9,900 股 vs 旧逻辑 18,900 股） |
| F6 | 同日重复信号可能重复下单 | 以 `(code, signal_date, model_version)` 去重；重复计入 `skipped.duplicate` 并写告警 | `test_duplicate_signals_are_idempotent` |
| F7 | 持有期口径混乱 | Trade 新增 `signal_date / exit_trigger_date / exit_fill_date / holding_trading_days`；`exit_date` 保留为成交日 | `test_trade_records_signal_trigger_and_fill_dates` |
| 账本 | 逐笔 pnl 四舍五入导致残差 0.07 元 | 新增 `pnl_exact` / `unrealized_pnl_exact`，对账用未舍入值 | 恒等式残差 ≤0.01 元 |

价格域相关测试：`tests/test_trend_m63_simulator.py`（12 项）、`tests/test_trend_m63_pricing.py`（2 项）。

---

## 3. 整改后核对（A2/B2，`RAW_APPROX`）

| 模型 / 规则 | 账本最大误差 | 负现金 | 日期顺序 | 整手 | 缺费用 | 未估值持仓 | 状态 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| A / A_fixed | 0.00 | 0 | 0 | 0 | 0 | 0 | PENDING_REVIEW |
| A / E_combo | 0.00 | 0 | 0 | 0 | 0 | 0 | PENDING_REVIEW |
| B / A_fixed | 0.00 | 0 | 0 | 0 | 0 | 0 | PENDING_REVIEW |
| B / E_combo | 0.00 | 0 | 0 | 0 | 0 | 0 | PENDING_REVIEW |
| A+B / A_fixed | 0.00 | 0 | 0 | 0 | 0 | 0 | PENDING_REVIEW |
| A+B / E_combo | 0.00 | 0 | 0 | 0 | 0 | 0 | PENDING_REVIEW |

`all_invariants_ok: true`；`已实现 + 未实现 = 权益变化` 残差 ≤0.01 元。

---

## 4. 与 M6.1 的差异（F4 的影响）

| 模型 / 规则 | M6.1 收益 | M6.3 收益 | Δpp | M6.1 回撤 | M6.3 回撤 | Δpp | 交易数变化 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A / A_fixed | -19.87% | -19.88% | -0.01 | -25.57% | -25.54% | +0.03 | 0 |
| A / E_combo | -19.03% | -19.02% | +0.01 | -22.45% | -22.42% | +0.03 | 0 |
| B / A_fixed | -3.61% | **-4.82%** | **-1.21** | -22.47% | -23.02% | -0.55 | 0 |
| B / E_combo | -0.15% | -0.01% | +0.14 | -24.50% | -24.38% | +0.12 | 0 |
| A+B / A_fixed | -12.34% | -12.23% | +0.11 | -24.67% | -24.55% | +0.12 | 0 |
| A+B / E_combo | **+0.34%** | **-3.69%** | **-4.03** | -14.96% | -14.95% | +0.01 | -1 |

**关键结论**：

1. M6.1 唯一为正的 `A+B / E_combo +0.34%` 在消除日内前视后变成 **-3.69%**；
2. 该差异完全由 F4（仓位计算基准）导致，不属于「策略变差」，而是**原先的正收益建立在不可执行的信息上**；
3. 因此 M6.1 中「组合收益改善」的表述必须撤回；M6.2 已判定其不是 alpha，本轮给出量化证据；
4. 这些数字仍然只是 `RAW_APPROX` 下的工程核验，**不得作为新的策略收益结论**。

---

## 5. 公司行为账本

`corporate_action_ledger_reconciliation.csv`：6 组运行全部为
`events_in_window=0`、`status=MISSING_CORPORATE_ACTION_DATA`。

- 未生成任何虚拟分红/送转/配股现金流；
- 每笔交易的 `corporate_action_status=UNKNOWN_NO_EVENT_DATA`；
- 因此即使账本恒等，也**不能**声称已正确处理公司行为。

---

## 6. Batch 2 门禁

| 项 | 结果 |
| --- | --- |
| F3 长期停牌安全处理 | 通过（标记不可估值，不再静默变现） |
| F4 日内前视 | 通过（新增测试证明） |
| F6 重复信号幂等 | 通过 |
| F7 日期口径 | 通过 |
| 账本恒等（逐笔/逐日） | 通过（残差 ≤0.01 元） |
| 双价格域类型隔离 | 通过（`ADJUSTED` 需显式 signal_bars） |
| 真实复权与公司行为 | **未通过**（数据缺失） |
| 交易级结论可用性 | `PENDING_REVIEW` |

Batch 2 工程整改通过，但因价格域数据缺失，**G9 仍不通过**。
