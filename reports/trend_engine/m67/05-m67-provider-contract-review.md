# M6.7 供应商契约与授权守卫审查

- 代码：`backend/app/services/trend_engine/authz.py`（新增）
- 既有：`backend/app/services/trend_engine/multisource.py`（M6.5）、`budget_guard.py`（M6.6）
- 测试：`tests/test_trend_m67_authorization_guard.py`（15 项）

---

## 1. 三层护栏

| 层 | 模块 | 作用 |
| --- | --- | --- |
| 注册/协议层 | `multisource.ProviderRegistry` | 未授权 provider 无法取得适配器 |
| 请求预算层 | `budget_guard.RequestBudgetGuard` | 100 次硬上限、预占计数、fail-closed |
| 数据用途层 | `authz.AuthorizationGuard` | provider × dataset × purpose × storage 逐维判断 |

三层彼此独立：即使某层被绕过，另外两层仍会拒绝。

## 2. AuthorizationGuard 判定顺序

```
台账无行 → NO_LEDGER_ROW
status ∈ {NOT_AUTHORIZED, REJECTED, UNKNOWN, EXPIRED, PENDING_*} → 拒绝
evidence_uri 为空 → EVIDENCE_URI_MISSING
approved_by / approval_date 为空 → APPROVER_OR_DATE_MISSING
expires_at < today → EXPIRED
storage=RAW_LOCAL 且未授权 → RAW_STORAGE_NOT_ALLOWED
purpose=DERIVED_METRICS 且未授权 → DERIVED_METRICS_NOT_ALLOWED
purpose=INTERNAL_WEBSITE 且未授权 → INTERNAL_WEBSITE_NOT_ALLOWED
purpose=PUBLIC_DISPLAY 且未授权 → PUBLIC_DISPLAY_NOT_ALLOWED
purpose=REDISTRIBUTION 且未授权 → REDISTRIBUTION_NOT_ALLOWED
```

## 3. 职责分离（本阶段必须保持）

| 组合 | 结果 |
| --- | --- |
| 允许个人研究 + 拒绝网站 | 通过（M6.6 所有第三方数据即此状态） |
| 允许 API 调用 + 拒绝本地存储 | 通过（`RAW_STORAGE_NOT_ALLOWED`） |
| 允许本地存储 + 拒绝公开展示 | 通过 |
| 允许衍生指标 + 拒绝再分发 | 通过 |

## 4. Fake Provider

`authz.FakeProvider`：

- `capabilities()["data_origin"] == "SYNTHETIC_FIXTURE"`；
- `network_enabled == False`；
- `fetch()` 返回 fixture，`network_calls` 恒为 0；
- 不得混入真实研究数据或生产路径。

## 5. 与台账的衔接

`AuthorizationRecord` 字段与 `reports/trend_engine/m66/provider_approval_ledger.csv` 一一对应。当前加载真实台账时，**所有第三方均为拒绝**——这是预期行为，不是配置错误。
