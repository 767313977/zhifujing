# M6.7 PIT 时间语义契约

实现：`data_contracts.PitTimestamps` + `evaluate_pit_eligibility`

---

## 1. 五类时间

| 字段 | 定义 | 禁止替代 |
| --- | --- | --- |
| `effective_at` | 事实生效时间（如除权日） | 不得当公告时间 |
| `announced_at` | 公开公告时间 | 不得用除权日代替 |
| `available_at` | 研究/策略系统最早可获得时间 | **不得用 `ingested_at` 代替** |
| `ingested_at` | 本系统采集入库时间 | 不得当可得时间 |
| `retrieved_at` | 本次请求取得时间 | 不得当历史可得时间 |

## 2. 判定

```
available_at 缺失            → PIT_UNVERIFIED（禁止严格 OOS）
announced_at 只有日期(≤10位) → DATE_LEVEL_ONLY（禁止公告时点策略）
ingested_at < available_at   → INGESTED_BEFORE_AVAILABLE（数据可疑）
全部满足                     → PIT_OK
```

## 3. 各数据集的 PIT 现状

| 数据集 | announced_at | available_at | 判定 |
| --- | --- | --- | --- |
| iFinD 日行情 | 不适用 | 未提供 | `PIT_UNVERIFIED` |
| iFinD 公司行为 | 日期级（且多为空） | 未提供 | `PIT_UNVERIFIED` |
| iFinD 板块成分 | 不适用 | 未提供 | `PIT_UNVERIFIED` |
| Tushare（文档） | `ann_date` 日期级 | 按更新批次时间 | 文档级，未验证 |
| 理杏仁（文档） | `date` 日期级 | 未提供 | 文档级，未验证 |

## 4. 对回测的约束

1. 无 `available_at` 的数据**不得**用于「无前视」证明；
2. 日期级公告只能支撑「次日开盘」类策略，不能支撑「公告时点」策略；
3. 当前快照不得回填历史（见 G3 契约）；
4. 事后修订（`revision_status`）必须保留版本，不能只留最新值。

## 5. 与 Gate 的关系

G9-PIT 与 G3 的 PIT 子项在取得 `available_at` 证据前保持 **FAIL / PIT_UNVERIFIED**。
