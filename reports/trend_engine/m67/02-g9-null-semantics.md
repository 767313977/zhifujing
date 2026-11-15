# M6.7 G9 空值语义

空值是数据契约里最容易出错的地方：**`0` 是值，`None` 是缺失，`不适用` 是业务定义，`来源不支持` 是供应商能力，`未核实` 是证据不足。**

---

## 1. 六种状态

| 枚举 | 含义 | 典型场景 | 允许进入账本 |
| --- | --- | --- | --- |
| `PROVIDED` | 有值（含 0） | 每股派现 23.0；送转比例 0.0 | 是 |
| `NOT_PROVIDED` | 供应商未提供该字段值 | iFinD 分红金额为空 | 否（记缺失） |
| `NOT_APPLICABLE` | 业务上不适用 | 配股事件的送股比例 | 是（按不适用展示） |
| `EXPLICIT_ZERO` | 明确为零 | 公告“不分配不转增” | 是（0 参与计算） |
| `DATA_MISSING` | 应有但缺失 | 该有分红却整行为空 | 否 |
| `SOURCE_UNSUPPORTED` | 供应商不提供该能力 | iFinD 无历史板块成分 | 否 |
| `UNVERIFIED` | 有值但无法核实 | 口头答复给出的数字 | 否 |

## 2. 判定函数

```python
classify_missing_value(value, reason=None)
```

- `None` / 空串 → `NOT_PROVIDED`；
- `0` / `0.0` → `EXPLICIT_ZERO`；
- `reason="NOT_APPLICABLE"` → `NOT_APPLICABLE`（优先级最高）；
- `reason="SOURCE_UNSUPPORTED"` / `"DATA_MISSING"` / `"UNVERIFIED"` → 对应枚举。

## 3. 禁止事项

| 禁止 | 原因 |
| --- | --- |
| `NULL → 0` | 会把「未披露」算成「不分红」，直接改变现金流 |
| `NULL → 前值填充` | 会把停牌/缺失伪装成有效交易日 |
| `缺失 → 均值/中位数` | 会污染横截面排名与 RPS |
| 空字符串当 0 | 类型混淆 |
| 用 `ingested_at` 填 `available_at` | 破坏 PIT |

## 4. 在生成事件账本时的用法

1. 逐字段调用 `classify_missing_value`；
2. `PROVIDED` / `EXPLICIT_ZERO` 才进入求和；
3. `NOT_APPLICABLE` 记 0 但**标注原因**，不参与求和；
4. 其余状态一律进入 `missing_fields`，并让记录 `quality_status ≤ PARTIAL`。

## 5. 测试锚点

- `tests/test_trend_m67_g9_contract.py::test_null_zero_and_not_applicable_are_distinct`
- `::test_cash_dividend_missing_amount_blocks_verified`
- `::test_rights_issue_missing_shares_not_derived`
