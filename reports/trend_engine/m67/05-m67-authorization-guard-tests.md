# M6.7 授权守卫测试说明

- 测试文件：`tests/test_trend_m67_authorization_guard.py`（15 项）
- 被测模块：`backend/app/services/trend_engine/authz.py`
- 网络请求：**0**（FakeProvider 不联网）

## 1. 覆盖矩阵

| 条件 | 测试 | 结果 |
| --- | --- | --- |
| 无台账行 | `test_no_ledger_row_denied` | 拒绝 |
| `NOT_AUTHORIZED` / `REJECTED` / `UNKNOWN` / `EXPIRED` / `PENDING_*` | `test_non_authorized_status_denied`（参数化） | 拒绝 |
| 证据 URI 缺失 | `test_missing_evidence_uri_denied` | 拒绝 |
| 批准人缺失 | `test_missing_approver_denied` | 拒绝 |
| 许可过期 | `test_expired_license_denied` | 拒绝 |
| 个人研究通过、网站拒绝 | `test_personal_research_allowed_but_website_denied` | 分离 |
| API 可用≠可本地存储 | `test_raw_storage_requires_explicit_permission` | 拒绝 RAW |
| 衍生指标需单独许可 | `test_derived_metrics_requires_permission` | 拒绝 |
| 再分发需单独许可 | `test_redistribution_requires_permission` | 拒绝 |
| `require()` 抛异常 | `test_require_raises` | `AuthorizationDenied` |
| 全授权可展示 | `test_full_authorization_allows_display` | 通过 |
| 非法用途/存储 | `test_invalid_purpose_and_storage_rejected` | `ValueError` |
| FakeProvider 合成标记 | `test_fake_provider_is_synthetic_and_never_calls_network` | 0 网络 |
| 汇总计数 | `test_summary_counts` | 1 允许 / 2 拒绝 |

## 2. 与真实台账的联调（未执行）

若把 M6.6 的 18 行台账载入 `AuthorizationGuard`，预期结果：

- 4 条 iFinD `AUTHORIZED_LIMITED`：个人研究用途可过，网站/展示/再分发全部拒绝；
- 13 条 `NOT_AUTHORIZED`：全部拒绝；
- 1 条 `REJECTED`（iFinD 无成分接口）：拒绝。

本阶段未写联调脚本，因为台账字段与 `AuthorizationRecord` 需要一次显式映射（`approval_status` 等列名一致，但 `dataset` 列名不同）。该映射留待 M6.8，且必须先经用户批准。
