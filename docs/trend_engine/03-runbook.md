# Trend Engine 运行手册（M1～M6）

## 1. 环境

```powershell
cd C:\Users\76731\Documents\trae_projects\fupan
.\.venv\Scripts\python.exe -V   # 3.13
```

后端测试不需要外部网络；回测只读本地 SQLite。

## 2. 单元 / 回归测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  tests\test_trend_indicators.py `
  tests\test_trend_model_a.py `
  tests\test_trend_model_b.py `
  tests\test_trend_sector_market.py `
  tests\test_trend_risk.py `
  tests\test_trend_data_quality.py `
  tests\test_trend_backtest.py
```

相关回归（口径共用）：

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  tests\test_research_screen.py tests\test_research_backtest.py `
  tests\test_trend_template.py tests\test_scan_rps.py `
  tests\test_sector_rps.py tests\test_pools.py tests\test_pools_trend.py
```

## 3. 历史回测

**只读**，不会写任何数据库：

```powershell
.\.venv\Scripts\python.exe scripts\backtest_trend_engine.py `
  --db backend\data\b1_research.db `
  --days 240 --models A,B --top 20 --cost-bps 15 `
  --bootstrap 1000 --collect-sector `
  --report-dir reports\trend_engine\m6
```

产物：

| 文件 | 内容 |
| --- | --- |
| `m6_model_a.json` / `m6_model_b.json` | 全区间 + 训练/验证/样本外 + 分环境 + bootstrap |
| `m6_combined.json` | A∪B 组合 |
| `m6_parameter_sensitivity.json` | RPS 0/5、Top10/30、阈值 50/70、板块开关 |
| `m6_walk_forward.json` | 前块选参、后块评估 |
| `m6_regime.json` / `m6_tail.json` | 环境分层、尾部依赖 |
| `m6_data_quality.json` | 库覆盖与限制 |
| `m6_report.md` | 汇总与 10 个结论 |

## 4. 常用变体

```powershell
# 关闭 RPS 权重（对照）
... --rps-points 0
# 开启板块 5 分（只在有 sector_member 的 60 天窗口做对照）
... --sector-points 5 --collect-sector
# 长历史库（只有 stock_daily，无 RPS/板块/指数）
... --db backend\data\history.db --days 240
```

`history.db` 的限制：无交易日历（退化为行情日期并集）、无 RPS、无板块、无指数、
**无退市股** → 结论只能标「现存股票样本研究」。

## 5. 结果阅读规则

1. 先看 `execution_samples`；<200 的档一律 `UNKNOWN`；
2. `observation`（含退出跌停）与 `execution`（可成交）必须分开读，不能把观察收益当可交易收益；
3. `excess_mean` 是相对同日同持有期全市场等权的超额；
4. `tail.top5_share` > 0.5 说明收益高度依赖少数极端样本；
5. `bootstrap` 的 95% 区间跨 0 → 不能宣称有效；
6. 市场环境只有指数口径时（`breadth` 为空），环境结论只作分层参考。

## 6. 数据窗口（2026-11-15 实测）

| 库 | 个股日线 | RPS | 板块 | 指数 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `b1_research.db` | 2024-09-02～2026-10-09（506 天） | 251 天（2025-09-22～10-21，含混版本日） | 255 天（至 09-24），成分股 60 天 | 255 天（至 09-24） | 主回测库 |
| `history.db` | 2021-09-14～2026-10-19（1230 天） | 无 | 无 | 无 | 仅现存股票 |
| `b2_final_snapshot.db` | 同 b1 | 同 b1 | 同 b1 | 同 b1 | 快照副本 |

## 7. M7 / M8 设计边界（本批次**不实施**）

- M7：新增 5 张表 + `trend` 修订域 + 15:05 尾部链挂载；幂等、数据不完整不生成正式排名；
- M8：`/research/trend` 页面，必须使用 `Layout`，复用 `Panel/Segmented/EChart/KLineChart`；
- 两者都必须在 P0 F1（`data_revision` 安装）与 M6 结论达标之后才进入实施。
