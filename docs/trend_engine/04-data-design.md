# Trend Engine 数据设计（M7/M8 前置，未实施）

> 本批次**不建表、不写库**。以下是设计，需单独授权后才实施。

## 1. 复用的现有表

| 表 | 用途 | 注意 |
| --- | --- | --- |
| `stock_daily` | 日线（不复权 + 真实 pct_chg） | 前复权在内存里由 `build_bars` 生成 |
| `rps_daily` | RPS20/60/120/250 | 混版本日必须用 `data_quality.rps_day_status` 拒绝 |
| `sector_daily` / `sector_member` | 板块收益 / 成分股 | 成分股广度仅在 manifest complete 时可信 |
| `pattern_hit` | 形态命中 | 只有 26 个命中日，不能当连续信号源 |
| `index_daily` / `breadth_daily` / `market_sentiment` / `fear_greed_daily` | 市场环境 | b1 的 breadth 为空；指数止 09-24 |
| `trade_calendar` | 交易日 | **预置到 2026-12-31**，未来日期不能当数据边界 |

## 2. 新增表（5 张）

### 2.1 `trend_model_versions`

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `model_version` | TEXT PK | 如 `A-1.0.0` |
| `model_name` | TEXT | `trend_breakout` / `trend_continuation` |
| `params_json` | JSON | 全部阈值与权重 |
| `calculation_version` | TEXT | `trend-engine-1.0.0` |
| `status` | TEXT | `research` / `validated` / `retired` |
| `created_at` | DATETIME | |

只增不改。

### 2.2 `trend_daily_scores`

主键 `(trade_date, code, model_name, model_version)`。

列：总分、5 个分项（structure / breakout|pullback / volume / rps / sector）、
RPS20/60/120/250、MA20/60/120、ATR14、RET20/60、回撤、成交额、
`data_quality_status`、`reasons_json`、`exclusions_json`、`created_at`。

写入：后台盘后任务幂等 upsert；同一 `(trade_date, model_version)` 重跑结果必须一致。

### 2.3 `trend_signal_snapshots`

主键 `(trade_date, model_name, code, model_version)`，含 `rank`、`pool`、
`sector_code`、`market_regime`、`created_at`。

**历史快照不可被覆盖**：换版本新增行，不更新旧版本。

### 2.4 `trend_pool_members`

主键 `(trade_date, pool, code)`；`pool ∈ {start, continuation, watch, warning, broken}`。
每日替换当天、历史保留；`exit_reason` 记录出池原因。

### 2.5 `trend_backtest_results`

主键 `run_id`（参数哈希）；`strategy`、`params_json`、`start`、`end`、
`horizons_json`、`metrics_json`、`data_version`、`created_at`。只增不改。

## 3. 数据修订域

```python
# data_revision.DOMAINS 追加
"trend": ("trend_daily_scores", "trend_signal_snapshots", "trend_pool_members"),
```

安装：`scripts/install_data_revision.py --db <isolated> --strict`；
验收：域数 8→9、触发器 24→33、`--status` 不为不存在的库建空文件、重复安装不换 `db_uuid`。

## 4. 写入边界

- 普通用户请求只读（`get_research_db` + `PRAGMA query_only`）；
- 后台任务通过授权研究写入路径保存；
- `require_research_writable` 在灰度只读形态下必须返回 503；
- 生产安装/写入需单独授权，回滚保留备份与原子切换步骤（见 M0 审计 §7）。
