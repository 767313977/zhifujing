# M6.3 Batch 1：公司行为模型

- 状态：**设计完成，数据不可用**。
- 依据：`corporate_action_audit.json`（库内无因子/公司行为表；`pct_chg` 与 raw close 11.94% 不一致）。

---

## 1. 事件分类

| 类型 | 说明 | 对账户的影响 |
| --- | --- | --- |
| `CASH_DIVIDEND` | 每股现金分红 | 现金流入；除权日价格下调；需代扣税规则（若模拟） |
| `STOCK_DIVIDEND` | 送股 | 持股数增加；单位成本下降；无现金 |
| `CAPITALIZATION` | 转增 | 同送股 |
| `RIGHTS_ISSUE` | 配股 | 需缴款；未实现认购机制时不得自动获得股份 |
| `SPLIT` / `MERGE` | 拆股 / 并股 | 股数与单位成本按比例调整 |
| `DELIST` | 退市 | 进入 `DELISTED`，不得按最后价当作可变现资产 |

---

## 2. 时间轴（必须分开）

| 字段 | 含义 | 用途 |
| --- | --- | --- |
| `announcement_at` | 公告可知时间 | PIT 判断：在此之前不得使用该事件 |
| `record_date` | 股权登记日 | 谁有权获得 |
| `ex_date` | 除权除息日 | 价格调整起点；涨跌停参考价切换 |
| `pay_date` | 现金到账日 | 现金流水入账 |
| `share_date` | 股份到账/可交易日 | 持股数变化 |
| `effective_date` | 业务生效日（通常等于 ex_date） | 复权因子变化的锚点 |

**禁止**：把复权因子变化直接当作现金流；把 `ex_date` 当作 `announcement_at`；把 `pay_date` 的现金提前到 `ex_date`。

---

## 3. 事件记录结构

```python
@dataclass(frozen=True)
class CorporateAction:
    code: str
    action_type: str                 # 上表枚举
    announcement_at: datetime | None
    record_date: date | None
    ex_date: date
    pay_date: date | None
    share_date: date | None
    cash_per_share: Decimal | None   # 税前
    shares_per_share: Decimal | None # 送转比例
    rights_ratio: Decimal | None     # 配股比例
    rights_price: Decimal | None
    source: str
    source_version: str
    revision_id: str
    is_final: bool
    payload_hash: str
    quality_status: str              # OK / PARTIAL / UNKNOWN / SUSPECT
```

主键建议：`(code, ex_date, action_type, revision_id)`。

---

## 4. 账本与会计规则

### 4.1 现金分红

```
应收现金 = 持仓股数 × 每股派现（税前，按适用税率扣减若模拟）
除权日：持仓市值按 raw 价格自然下降；不得同时记一笔「价格损失」和「现金收益」
到账日：cash += 应收现金；receivables -= 应收
```

禁止在 `ex_date` 与 `pay_date` 重复计入。若没有税率规则，必须记录 `tax_status=UNKNOWN`。

### 4.2 送股 / 转增

```
新增股数 = 持仓股数 × 送转比例（向下取整到 1 股）
新成本基础 = 原成本 / (1 + 比例)
```

只改股数与单位成本，**不得**记为现金收益。

### 4.3 配股

- 若模拟器不支持缴款认购：持股数不变，记录 `rights_unexercised=true`，并在结果中标注「未参与配股」；
- 若支持：在缴款日 `cash -= 配股价 × 配股数`，股数与成本同步调整；
- **不得**默认免费获得配股股份。

### 4.4 拆并股

按比例调整股数与单位成本；现金不变。

### 4.5 退市 / 长期停牌

见 Batch 2 状态机（`DELISTED` / `UNVALUABLE`）。没有退市回收率数据时，不得编造回收金额。

---

## 5. 与复权因子的关系

| 项 | 是否可由因子推出 |
| --- | --- |
| 复权价格序列 | 是（因子 × raw） |
| 现金分红金额 | **否**（因子只反映价格比例） |
| 送转股数 | **否** |
| 配股缴款 | **否** |
| 到账日期 | **否** |

因此「有因子」不等于「有公司行为账本」；两者必须分别取得。

---

## 6. 当前可用性

| 数据 | 状态 |
| --- | --- |
| 复权因子 | `MISSING` |
| 分红 / 送转 / 配股明细 | `MISSING` |
| 公告时间 | `MISSING` |
| 除权日 | 只能从「raw 与 pct 不一致」推断，**类型不可判定** |
| 退市事件 | `MISSING` |

处理策略：

1. 模拟器**不生成**任何虚拟分红或送转；
2. 若某持仓区间跨越疑似除权日，该笔交易标记 `corporate_action_status=UNKNOWN`，组合结果标记 `PENDING_REVIEW`；
3. 账本中保留 `corporate_action_ledger` 空表结构，待数据接入后启用。

---

## 7. Batch 1 结论

| 项 | 结果 |
| --- | --- |
| 事件模型 | 完成 |
| 时间轴区分 | 完成 |
| 会计规则 | 完成（含禁止项） |
| 真实事件数据 | **不可用** |
| 交易级可用性 | 维持 `PENDING_REVIEW` |
