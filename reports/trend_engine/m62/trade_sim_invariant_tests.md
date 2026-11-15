# M6.2 Batch 4：交易模拟器不变量测试清单

- 测试文件：`tests/test_trend_trade_sim.py`（12 项）、`tests/test_trend_m62_tools.py`
- 实盘数据核对：`scripts/m62_trade_sim_audit.py` → `trade_sim_audit.json`
- 结果：**20 passed**（`test_trend_trade_sim.py` + `test_trend_m61_tools.py`）；Ruff 通过。

---

## 1. 合成数据不变量（单元测试）

| # | 不变量 | 测试 | 断言 |
| ---: | --- | --- | --- |
| 1 | 信号日收盘不成交，最早 T+1 开盘买入 | `test_buy_executes_next_open_not_signal_close` | `entry_date == 信号日 + 1`；`entry_price == 当日开盘` |
| 2 | 持有期结束在收盘判定、次日开盘卖出 | `test_horizon_exit_is_next_open` | `exit_date == 触发日 + 1`，`reason == horizon` |
| 3 | 开盘涨停不能买入 | `test_limit_up_blocks_buy` | 无成交，`skipped.limit_up ≥ 1` |
| 4 | 停牌延迟买入 | `test_suspension_delays_entry` | `skipped.suspended ≥ 1`，入场顺延到复牌日 |
| 5 | 成本必须使平价交易亏损 | `test_costs_reduce_pnl` | `pnl < 0` |
| 6 | 最大持仓与现金分配 | `test_max_positions_and_cash_allocation` | 交易数 ≤ 上限，`skipped.slots ≥ 1` |
| 7 | ATR 止损只在收盘破位后触发 | `test_atr_stop_exits_after_close_break` | `reason == atr_stop` |
| 8 | 停牌期间市值不能用 0 | `test_suspended_position_keeps_last_price` | 停牌日权益不塌陷 |
| 9 | MA 窗口不足不误触发 | `test_ma_exit_reason` | 无 `ma_break` |
| 10 | **逐日账本恒等式** `cash + market_value == equity` | `test_daily_ledger_identity_holds` | 每日误差 < 0.02，现金不为负 |
| 11 | **逐笔盈亏含双边费用** | `test_trade_pnl_includes_both_side_fees` | `pnl == -(买入佣金 + 卖出佣金/印花税)`；无持仓时累计盈亏 == 权益变化 |
| 12 | **T+1 与整手** | `test_trade_dates_respect_t_plus_one` | `exit > entry > signal`；`shares % 100 == 0` |

---

## 2. 真实数据不变量（A2/B2 冻结信号，b1 只读库）

对 A_fixed 与 E_combo 共 6 组（A / B / A+B）执行：

| 检查 | 阈值 | 结果 |
| --- | --- | --- |
| `cash + market_value - equity` 最大误差 | < 0.02 | **0.00**（全部 6 组） |
| 负现金天数 | 0 | 0 |
| 卖出早于买入 / 同日 | 0 | 0 |
| 非整手交易 | 0 | 0 |
| 买入佣金缺失（`entry_fee ≤ 0`） | 0 | 0 |
| `已实现盈亏 + 期末浮盈` 与权益变化之差 | ≤ 0.05 元 | 0.01 ~ 0.05 元（四舍五入） |
| M6.1 报告收益/回撤复现 | 差 ≤ 0.01pp | 6/6 一致 |

---

## 3. 未被测试覆盖的行为（明确列出）

| 行为 | 原因 | 风险 |
| --- | --- | --- |
| 复权价格下的成交与涨跌停 | 现库无复权因子表 | HIGH：除权日结果失真 |
| 长期停牌超过 `max_exit_delay` 后的持仓处置 | 需求未定义 | MEDIUM：净值可能被高估 |
| 同日重复信号去重 | 现有信号构造已按 `(day, code)` 合并，但模拟器本身不去重 | LOW |
| 盘中触发路径（最低价止损） | 仅日线级模拟 | 未声称支持 |
| 分红送转、配股 | 无公司行为数据 | HIGH（与复权同源） |
| 冲击成本曲线 | 无分钟数据 | 未声称支持 |

---

## 4. 结论

- 模拟器的**会计恒等式、T+1、成本、整手、仓位约束**均已通过可重复测试；
- **价格口径（复权）与公司行为**未通过，按 Batch 4 门禁，所有交易级结果保持 `PENDING_REVIEW`；
- 上述 6 项「未覆盖行为」在数据补齐前不得声称已模拟。
