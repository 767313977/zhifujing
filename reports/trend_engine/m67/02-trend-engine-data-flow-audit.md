# M6.7 N2：趋势引擎数据调用链审计

- 日期：2026-11-16
- 方法：只读全仓检索 + 关键模块逐段阅读；未运行任何策略、未改动生产代码
- 明细：`02-data-consumer-matrix.csv`（12 条数据流）、`02-risk-register.csv`（11 条风险）

---

## 1. 调用链总览

```
stock_daily(raw) ──┐
pct_chg ───────────┴─► patterns.build_bars (patterns.py:1107)
                          │  用 pct_chg 复利合成“前复权”net 序列
                          ▼
        trend_structure / momentum / volume_price / continuation_model
        breakout_model / risk_engine / screening / scoring
                          ▼
      trade_sim.run_backtest（执行价用 raw，信号价需显式 signal_bars）
                          ▼
                API / 报告导出
```

板块链路：

```
sector_daily ──► sector_strength._sector_days（Ret20/60、RPS、排名）
sector_member ─► 广度 up_ratio（仅在 manifest 完整时返回，否则 None）
```

## 2. 关键发现

### 2.1 生产信号价是「pct_chg 合成的前复权」，不是因子复权

`patterns.build_bars`（`backend/app/services/patterns.py:1107`）文档明确：

> 把一只票的**不复权**日线记录转成前复权序列……
> 收盘序列 `net[i] = net[i-1] * (1 + pct[i]/100)`；当日 OHLC 用当天收盘价做基准换算。

该函数被 **8 处**调用：`pools.py:549`、`jobs/scan_patterns.py:1003`、`jobs/intraday_b1.py:157`、`api/stock.py:667`、`services/research_screen.py:670`、`services/trend.py:148`、`services/stock_phase.py:192`。

含义：生产环境的「复权」是一套**基于涨跌幅复利的近似**，没有复权因子、没有方向、没有基准日血缘。它与 M6.3 设计的 `pricing.SignalBar`（要求显式因子）是**两套并行实现**。

判定：这是 R-01（P1）。它不构成 bug——代码注释说明了理由与实测依据——但**不能满足 G9-PRICE 的准入要求**，因为无法审计因子来源。

### 2.2 `pricing.py` 的价格域契约未被生产使用

- `PriceBar` / `SignalBar` / `ExecutionBar`（`pricing.py:35-75`）只被 `pricing.py` 自身构造；
- 全仓只有 `trade_sim.py:30` 引用了 `PriceDomainError`；
- 没有生产模块构造 `SignalBar`。

判定：R-02（P1）。契约存在但未接线，文档不得写成「双价格域已落地」。

### 2.3 板块广度已有完整性闸门（正向）

`sector_strength.py:7-8` 明确：广度只在 `sector_member` 的 manifest 完整且与来源 Count 一致时才可信，否则 `up_ratio` / `strong_members` 一律 `None`——**不拿残缺成分冒充广度**。这是正确的 fail-closed 设计，记为 positive control，不列为风险。

### 2.4 ST 只能按「当前名称」识别

`limit_rules.py:127` 用 `"ST" in name.upper()` 判断，模块注释（77-79 行）自述「历史 ST 变更还原不了」。影响：历史回测的涨跌停档位可能错误。判定 R-04（P1），与 G9-PIT 相关。

### 2.5 未发现的反模式

| 反模式 | 结果 |
| --- | --- |
| `fillna(0)` / `bfill` / `ffill` | 在 `services/trend_engine/` **0 命中** |
| `shift(-n)`（未来函数） | **0 命中** |
| `except Exception: return []` 吞错 | `trend_engine/` 内无此模式；异常会向上抛 |
| 缓存键缺版本 | `cache_identity.py` 已定义四维键，但**未接入现有缓存**（R-10） |

### 2.6 未能验证项（标记 UNVERIFIED，不推断）

| 项 | 原因 |
| --- | --- |
| 停牌/缺日是否在调用方补齐 | 需逐调用方运行时验证，本阶段只读代码未跑策略 |
| `stock_universe` 是否有历史快照版本 | 表结构可见但语义未审计 |
| RPS 是否存在跨版本比较 | 依赖调用方传参，静态无法穷举 |

## 3. 对 G9 / G3 的净影响

| Gate | 影响 |
| --- | --- |
| G9-PRICE | 生产价格域**不能**作为 G9 通过证据（R-01/R-02） |
| G9-CA | 生产链路不消费公司行为表，无新增风险 |
| G9-PIT | ST 历史不可还原（R-04），PIT 判定需降级 |
| G3 | 生产只用当日板块快照；历史成分缺失不构成**新**风险，但也不提供历史能力 |
| G2 | 板块日线仍为区间/快照口径（R-05） |

## 4. 修复建议（不在本阶段实施）

1. **R-01**：接入可审计因子后，新增强制 `adjustment_type` 的 `SignalBar` 构造路径，并把 `build_bars` 明确降级命名为 `RAW_APPROX`；
2. **R-02**：在 `pricing.py` 顶部标注「参考契约，未接入生产」，或完成接线后再宣称落地；
3. **R-03**：`build_bars` 入口对 None 显式抛错；
4. **R-04**：接入历史 ST 状态表前，回测报告中标注档位不确定。

本阶段**不修改任何生产代码**，以上仅作为 M6.8 的输入。
