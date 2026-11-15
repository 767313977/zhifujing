# Trend Engine 指标口径（V1.0）

- 代码：`backend/app/services/trend_engine/indicators.py`
- 计算版本：`trend-engine-1.0.0`（`config.CALC_VERSION`）
- 数据前提：全部输入是 `patterns.Bars` 的**前复权**序列（`build_bars` 用 `pct_chg` 复利得到）
- 缺失语义：历史不足 / 非有限值 → `None`，**绝不返回 0**，由调用方显式处理

## 1. 均线

| 指标 | 函数 | 窗口 | 说明 |
| --- | --- | --- | --- |
| MA5/10/20/30/60/120/250 | `ma_last(close, n)` / `rolling_mean` | 交易日 | 窗口内必须全部有限；不足 n 根返回 None |
| 斜率（5 日） | `slope_pct(close, n, 5)` | MA(n) 相对 5 个交易日前的变化率（%） | 相对值，跨价位可比 |
| 长斜率（10 日） | `slope_pct(close, n, 10)` | 同上 | 避免只看一周噪声 |
| 归一化斜率 | `normalized_slope(close, n, 10, atr_pct)` | 斜率 / ATR% | 低波动股的高斜率更有信息量 |
| 排列 | `ma_alignment(close, (5,10,20,60))` | — | `bull` / `bear` / `mixed` / `insufficient` |
| 收敛度 | `convergence_pct(close, (5,10,20,60))` | — | (max MA − min MA) / mean MA ×100 |
| 趋势持续 | `trend_days_above(close, 20)` | ≤250 | 连续收盘 > MA20 的交易日数 |

**走平阈值**：`slope_pct` 绝对值 < `0.2%` 记 `flat`（`IndicatorConfig.slope_flat_pct`）。

## 2. 收益与 RPS

| 指标 | 实现 | 口径 |
| --- | --- | --- |
| RET5/10/20/40/60/120/250 | `ret_pct(bars.pct_chg, n)` → `rps.ret` | `pct_chg` 复利；窗口内有效行 ≥90%，否则 None |
| RPS20/60/120/250 | **不重算** | 读 `rps_daily`（`scan_rps` 统一口径：pct 复利 + 交易日截窗 + 中位秩） |
| RPS 改善 | `momentum.evaluate(..., rps_history)` | 当日分位 − N 日前分位（分位点） |
| RPS 连续改善 | 同上 | 从最新往回数连续上升天数（≤10） |

## 3. 波动与回撤

| 指标 | 函数 | 定义 |
| --- | --- | --- |
| TR | `true_range(high, low, close)` | max(高−低, |高−昨收|, |低−昨收|) |
| ATR14 / ATR20 | `atr` / `atr_last` | TR 的**简单均值**（与 `patterns._low_atr_growth` 一致，非 Wilder） |
| ATR% | `atr_pct` | ATR14 / 收盘 ×100 |
| 20/40/60 日最大回撤 | `max_drawdown_pct(close, n)` | 窗口内峰谷最大跌幅（%，≤0，按收盘） |
| 距最高收盘 | `off_high_close_pct(close, n)` | 最新收盘 / 窗口最高收盘 − 1 |
| 距最高价 | `off_high_high_pct(high, close, n)` | 最新收盘 / 窗口最高价 − 1 |
| 滚动高/低点 | `rolling_high/low(..., exclude_last=True)` | 平台高点**排除当日** |

## 4. 量价

| 指标 | 函数 | 分母 |
| --- | --- | --- |
| 量比（5 日） | `volume_ratio(volume, 5)` | 前 5 日均量，**不含当日** |
| 成交额比（20 日） | `amount_ratio(amount, 20)` | 前 20 日均额，**不含当日**，单位：元 |
| 上涨/下跌量能比 | `up_down_volume_ratio(close, volume, 20)` | 上涨日量之和 / 下跌日量之和（含当日） |
| 收盘位置 | `close_position(high, low, close)` | (收−低)/(高−低)，振幅 0 → None |
| 上影线占比 | `upper_shadow_pct(open, high, low, close)` | (高−max(开,收))/(高−低) ×100 |
| 异常放量 | `VolumePriceState.abnormal_amount` | 成交额比 > 8×（只标记，不加分） |
| 量价背离 | `VolumePriceState.divergence` | 价涨量缩 / 价跌量增 |

⚠️ 成交额放大**不等于**资金净流入；趋势模型不输出任何「净流入」字段。

## 5. 数据质量

`evaluate_quality(bars, day, min_bars)`：

- 日线不足 `min_bars`（模型 A/B 默认 130）→ `ok=False`；
- 最新一根不是目标日（停牌 / 漏采）→ `ok=False`；
- 收盘价 / 成交额含非有限值 → `ok=False`；
- 不合格的票**不进入评分**，不得用 0 分代替。

## 6. 与既有实现的关系

| 能力 | 复用 |
| --- | --- |
| 前复权 K 线 | `patterns.build_bars` / `build_bars_from_arrays` |
| 区间收益 | `services.rps.ret` |
| 涨跌停 / 北交所 | `services.limit_rules` |
| 交易日历 | `services.trading_days`（脚本内矩阵用 `trade_calendar`） |
| 形态 | 不重复实现；Model A/B 只复用底层的突破/回踩思路 |
