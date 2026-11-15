# M6.7 供应商授权守卫实现说明

- 代码：`backend/app/services/trend_engine/authz.py`
- 测试：`tests/test_trend_m67_authorization_guard.py`（15 项，全部通过）
- 矩阵：`07-authorization-test-matrix.csv`

---

## 1. 三种拒绝的语义

| 拒绝原因 | 含义 | 用户可做的最小动作 |
| --- | --- | --- |
| `NO_LEDGER_ROW` | 台账里没有这条 provider×dataset | 先录入台账（含证据 URI） |
| `STATUS_*` | 状态不是 AUTHORIZED_* | 完成授权流程并更新状态 |
| `EVIDENCE_URI_MISSING` / `APPROVER_OR_DATE_MISSING` | 授权缺证据或批准人 | 补齐书面证据与批准记录 |
| `EXPIRED` | 许可过期 | 续期 |
| `RAW_STORAGE_NOT_ALLOWED` | 允许调用但不允许存原始数据 | 改为 `SUMMARY_ONLY` 或取得存储许可 |
| `INTERNAL_WEBSITE_NOT_ALLOWED` | 个人研究许可不能用于网站 | 取得网站内部使用许可 |
| `PUBLIC_DISPLAY_NOT_ALLOWED` | 不允许公开展示 | 取得展示许可 |
| `REDISTRIBUTION_NOT_ALLOWED` | 不允许再分发 | 取得再分发许可 |

## 2. 与现有模块的关系

```
multisource.ProviderRegistry   → 「能不能拿到适配器」
budget_guard.RequestBudgetGuard → 「还能发几次请求」
authz.AuthorizationGuard        → 「拿到数据后能不能这么用」
```

三层缺一不可。本阶段三层全部默认拒绝。

## 3. Fake Provider 的定位

- 只用于测试契约与守卫；
- 每条返回都带 `data_origin=SYNTHETIC_FIXTURE`；
- 不写入任何 staging、不进入研究库、不被任何生产模块引用；
- `network_calls` 恒为 0（测试断言）。

## 4. 未做与不做

| 项 | 原因 |
| --- | --- |
| 台账 CSV → `AuthorizationRecord` 自动加载 | 需要用户确认列映射；避免把 `AUTHORIZED_LIMITED` 误当可展示 |
| 真实 provider 适配器 | 无授权 |
| 许可到期自动提醒 | 属运维功能，不在本阶段 |
