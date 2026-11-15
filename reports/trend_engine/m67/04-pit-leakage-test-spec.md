# M6.7 PIT 前视泄漏测试规范

## 1. 目的

验证在「只用当时可得信息」的约束下，数据管线不会引入未来信息。

## 2. 测试设计（本阶段为规范，不含真实回测）

| ID | 场景 | 输入 | 期望 |
| --- | --- | --- | --- |
| L-01 | `available_at` 缺失的数据进入严格 OOS | 缺 available_at 的记录 | `PIT_UNVERIFIED`，拒绝进入 |
| L-02 | 用 `ingested_at` 顶替 `available_at` | ingested=2026-11-16，事件日=2026-06-26 | 判定为泄漏，拒绝 |
| L-03 | 用除权日当公告日 | announcement 为空、ex_date 有值 | 拒绝自动填充 |
| L-04 | 日期级公告当时间戳 | `2026-06-26` | `DATE_LEVEL_ONLY` |
| L-05 | 当前成分回填历史 | snapshot_date=2025-03-05, provider_asof=2026-11-16 | 拒绝 |
| L-06 | 事后修订覆盖原值 | revision_status=REVISED | 保留版本，历史点用旧值 |
| L-07 | 停牌日被前值填充 | 缺失交易日 | 拒绝填充（见 N2 R-01 注释） |
| L-08 | 当日收盘信号用于当日开盘成交 | signal_date = trade_date | 非法（需 T+1） |

## 3. 自动化状态

| 项 | 状态 |
| --- | --- |
| L-01 / L-02 / L-03 / L-04 / L-05 | 已由 `tests/test_trend_m67_g9_contract.py`、`test_trend_m67_g3_contract.py` 覆盖 |
| L-06 | 契约已定义字段，未接真实数据 |
| L-07 / L-08 | 属回测执行层，需 M6.8 接入 |

## 4. 判定

未通过 L-01~L-05 的数据集不得用于历史回测的无前视证明；G9-PIT 保持 `PIT_UNVERIFIED`。
