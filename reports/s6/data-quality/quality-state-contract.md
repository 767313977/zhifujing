# S6-C2 质量状态契约

## 1. 状态的唯一来源

后端：`backend/app/services/data_quality.py`

```python
QUALITY_VERSION = "data-quality-v2"
STATUS_VERIFIED = "VERIFIED_SOURCE"
STATUS_PARTIAL = "PARTIAL"
STATUS_UNVERIFIABLE = "UNVERIFIABLE"
STATUS_NOT_COLLECTED = "NOT_COLLECTED"
STATUS_PENDING = "PENDING"
STATUS_NOT_TRADE_DAY = "NOT_TRADE_DAY"
STATUS_SOURCE_ERROR = "SOURCE_ERROR"
```

各模块（`limit_funnel` / `sentiment_next_day` / `sector_resonance` /
`auction_anomaly` / `theme_intersection` / `stock_research_context` /
`research_screen`）都从这些常量派生，不各自硬编码同义字符串。

前端：`frontend/src/api/types.ts` 的联合类型 + `pages/DataQuality.tsx` /
`PostMarketWorkbench.tsx` / `AuctionAnomaly.tsx` / `LimitReview.tsx` 的标签映射。

## 2. 语义表

| 状态 | 含义 | 允许显示 | 禁止 |
| --- | --- | --- | --- |
| `VERIFIED_SOURCE` | 来源数据齐全且通过校验 | 数值 + `as_of` + 来源 | 推广到未采样的日期/股票 |
| `PARTIAL` | 部分样本/日期缺失，但已有部分可验证 | 「部分」+ 缺失数量 + 分母 | 显示成完整 |
| `UNVERIFIABLE` | 无法验证（依赖缺失/来源不可信） | 「不可验证」 | 显示成已验证或 0 |
| `NOT_COLLECTED` | 尚未采集 | 「未采集」 | 显示成「0 条」或「无事件」 |
| `PENDING` | 结果尚未产生（如 D+1 未到） | 「待定」 | 写 0 |
| `SOURCE_ERROR` | 查询/来源异常 | 「查询异常」+ reason code | 当成 0 或空数据 |
| `NOT_TRADE_DAY` | 非交易日 | 「非交易日」 | 计入覆盖率分母 |
| `NOT_COMPARABLE` | 对账双方不可比（本文件 §3） | 「不可比」+ reason_code | 转成 explained / 从分母删除 |
| `POST_CLOSE_UNKNOWN` | 盘后状态未知 | 「盘后未知」 | 推断为收盘值 |
| `POOL_CONFLICT` | 池归属冲突 | 「池冲突」 | 任选一个池当成事实 |
| `SCOPE_INCOMPLETE` | 范围不完整（已知缺口） | 「范围不完整」+ 已知/未知 | 与 `UNVERIFIABLE` 混用 |

## 3. 显示规则（前端契约）

1. `null` / `undefined` **不得**渲染成 `0`；必须显示对应状态标签。
2. 每个指标必须带 `as_of`、观察日期、最新资料截至日与来源。
3. 统计指标必须说明分母、样本范围、聚合口径与待完成数量。
4. 日期级、股票级、来源级、字段级质量不能互相冒充（例如不能用来源级
   `VERIFIED_SOURCE` 掩盖某只股票缺行）。
5. 质量失败时，相关结论不得进入推荐或策略排序。

## 4. 契约测试与对账

| 工具 | 作用 | 本轮结果 |
| --- | --- | --- |
| `scripts/check_s5_quality_consistency.py` | 真实库只读对账，`exit 2` 表示存在 `NOT_COMPARABLE`（fail closed） | `total=76 / passed=65 / explained=4 / not_comparable=7 / unexpected=0`，exit=2 |
| `tests/test_s5_quality_audit.py` | 后端质量状态单测 | 通过（全量回归中复跑） |
| `frontend/src/pages/__tests__/DataQuality.test.tsx` | 前端空态/未采集/异常标签 | 通过（全量回归中复跑） |

## 5. 本轮改动

- 无：C2 只做核对与登记，未修改质量判定逻辑。
- 7 条 `NOT_COMPARABLE` 原样保留（见 `not-comparable-register.md`）。
