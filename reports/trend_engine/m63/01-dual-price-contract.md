# M6.3 Batch 1：双价格域契约

- 审计脚本：`scripts/m63_corporate_action_audit.py`
- 证据：`corporate_action_audit.json`、`corporate_action_sample_reconciliation.csv`、`adjustment_factor_reconciliation.csv`
- 结论：**现库没有复权因子与公司行为数据，且 `stock_daily.pct_chg` 与 raw OHLC 在大样本上不一致 → 现有表已混用价格域。双价格域只能先落「严格拒绝」契约，无法完成真实因子核验。**

---

## 1. 现状审计

### 1.1 结构

| 检查 | 结果 |
| --- | --- |
| 含 `adj/factor/dividend/split` 等线索的表 | **0 个**（48 张表全量扫描） |
| `stock_daily` 列 | `trade_date, code, name, open, high, low, close, pct_chg, volume, amount, turnover` |
| `index_daily` / `sector_daily` / `limit_pool` 中的因子列 | 无 |
| `history.db` | 仅 `stock_daily`，无因子表 |
| iFinD 客户端 | 存在（`backend/app/sources/ifind.py`），但仓库内没有「历史复权因子 / 分红送转」已验证接口；本轮未发起批量取数（未授权、配额未知） |

### 1.2 `pct_chg` 与 raw close 不一致（关键发现）

用相邻两日 `raw_close` 与官方 `pct_chg` 反推除权参考价：

```
implied_ref = close / (1 + pct_chg / 100)
mismatch_pp = |implied_ref / prev_close - 1| × 100
```

| 区间 | 行数 | 占有效行比例 |
| --- | ---: | ---: |
| `mismatch > 0.03pp` | **304,188** | **11.94%**（分母 2,546,772） |
| `> 0.2pp` | 35,345 | 1.39% |
| `> 0.5pp` | 10,652 | 0.42% |
| `> 1pp` | 5,060 | 0.20% |
| `> 5pp` | 815 | 0.03% |
| `> 20pp` | 609 | 0.02% |

- 0.2–5pp 区间（约 3.5 万行）大量符合「现金分红导致 pct 按除权基准计算」的特征；
- >5pp（815 行）包含拆股、送转或数据异常（如 002594 2025-07-29：前收 337.0 → 收盘 111.42）；
- 0.03–0.2pp 区间（约 27 万行）无法与「低价股分位舍入」区分。

**含义**：`pct_chg` 很可能按**复权/除权基准**计算，而 OHLC 是原始价。直接拿 `pct_chg` 反推前收（现有 `trade_sim._prev_close` 与 `limit_rules._ref_price` 都在用）会在这些日期产生错误的涨跌停价与错误的收益。

### 1.3 真实样本（无法定因子）

`corporate_action_sample_reconciliation.csv` 列出 60 个最大跳变样本，字段包括：
`prev_close, close, pct_chg, implied_ref, mismatch_pp, gap_ratio_pct, band, action_type=UNKNOWN, factor_status=MISSING`。

`adjustment_factor_reconciliation.csv` 对前 20 个样本明确写 `adj_factor` 空、`status=UNVERIFIABLE`。

**没有因子就不能区分「现金分红 / 送转 / 配股 / 数据错误」**，因此 `action_type` 一律 `UNKNOWN`，不猜测。

---

## 2. 双价格域契约

### 2.1 类型

```python
@dataclass(frozen=True)
class PriceBar:
    code: str
    trade_date: date
    raw_open: Decimal
    raw_high: Decimal
    raw_low: Decimal
    raw_close: Decimal
    adjusted_open: Decimal | None
    adjusted_high: Decimal | None
    adjusted_low: Decimal | None
    adjusted_close: Decimal | None
    adj_factor: Decimal | None
    adjustment_method: str          # NONE / FORWARD / BACKWARD / UNKNOWN
    adjustment_reference_date: date | None
    adjustment_status: str          # OK / PARTIAL / UNKNOWN / INVALID_DATA
    source: str
    source_version: str
    available_at: datetime | None
    ingested_at: datetime | None
    corporate_action_revision_id: str | None
    quality_status: str
```

- `SignalBar`：只暴露 adjusted_*；`adjusted_status != OK` 时构造失败或返回 `INVALID_DATA`。
- `ExecutionBar`：只暴露 raw_*；成交、涨跌停、现金、持仓市值只用此类。
- 两类型不共享字段访问器，避免 `bar.close` 这种含义模糊的写法。

### 2.2 复权公式

```
adjusted_price = raw_price × factor_transform(adj_factor, method, reference_date)
```

- `method`、`factor_transform`、基准日必须由数据源文档 + 真实样本核验后固定；
- **当前状态 `UNVERIFIED`**：无因子数据，公式未与任何供应商定义对齐，禁止实现为默认因子 1.0；
- 缺因子：`adjusted_* = None`，`adjustment_status = "UNKNOWN"`，信号构建必须失败或返回 `UNKNOWN`，不得静默退化为 raw。

### 2.3 拒绝规则（必须实现）

| 情况 | 行为 |
| --- | --- |
| 因子缺失 | `INVALID_DATA` / `UNKNOWN`，拒绝进入指标 |
| 因子 ≤0 或非有限 | `INVALID_DATA` |
| 单日因子跳变 >50% 且无公司行为事件 | `SUSPECT`，该日信号价域不可用 |
| raw OHLC 非正或 high<low | `INVALID_DATA` |
| 同一 `(code, trade_date)` 重复 | 拒绝，取唯一来源前先报错 |
| 日期倒序 / 未来日期 | 拒绝 |
| `pct_chg` 与 raw 不一致且无公司行为记录 | 该日 `limit_rule_status=UNKNOWN`，不得套 10% 规则 |

---

## 3. 调用链审计

| 入口 | 当前价格来源 | 风险 |
| --- | --- | --- |
| `scripts/run_m61_trade_sim.py::load_bars_safe` | `stock_daily` raw OHLC | 信号指标与执行共用 raw，除权日失真 |
| `scripts/m62_trade_sim_audit.py` | 复用上述 loader | 同上 |
| `scripts/m62_reproduce.py` | 复用上述 loader | 同上 |
| `backend/app/services/research_backtest.py` | 从 `stock_daily` 构建矩阵（raw） | 收益/相对强度在除权日失真 |
| `patterns/指标`（MA/ATR） | raw | 除权日跳变 |
| `limit_rules` | raw close + `pct_chg` 反推 | 当 `pct_chg` 按复权基准时，反推参考价错误 |
| RPS（历史） | 由 close 计算，单位百分数 | 除权日收益错误，且混版本 |

**结论**：不存在「只改一个入口」的修复方案；必须先在数据层提供双价格，再逐一迁移。

---

## 4. Batch 1 门禁

| 门禁 | 结果 |
| --- | --- |
| 复权因子结构存在 | 否 |
| 真实样本 ≥3 个核对供应商定义 | **无法完成**（无因子、无公司行为表、无授权取数） |
| 公式已与供应商定义对齐 | 否（`UNVERIFIED`） |
| 双价格接口设计 | 完成（本文档） |
| 数据域混用已证明 | 是（11.94% 行不一致） |

按任务书第 10 节：**复权定义不清 → 停止「真实因子核验」**。因此：

- Batch 1 **工程产出通过**（契约、证据、样本）；
- **真实复权核验未通过**，进入 Batch 2 时只能实现「严格拒绝 + 执行用 raw」，不得声称已完成复权修复；
- 所有依赖复权价的信号指标继续标记 `UNKNOWN`，相关交易级结果保持 `PENDING_REVIEW`。
