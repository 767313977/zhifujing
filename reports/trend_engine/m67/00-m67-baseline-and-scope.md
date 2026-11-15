# M6.7 Batch 0：基线与阶段范围

- 日期：2026-11-16
- 阶段性质：**数据完整性与准入证据**阶段，不是模型优化阶段
- 上游：M6.6 `M66_PARTIALLY_ACCEPTED`，HEAD `b310d63`
- 工作分支：`feat/trend-engine-m67-data-gap-closure`
- 本阶段真实供应商请求：**0**
- 本阶段费用：**0 元**

---

## 1. 阶段目标

1. 把 M6.6 的 G9 / G3 失败原因拆成**可追踪的缺口清单**；
2. 保持 iFinD 28 问工单为 `DRAFT_NOT_SENT`，只做质量复核与 v2 版本；
3. 用公开文档与许可条款建立**按数据集 × 使用场景**拆分的供应商能力矩阵；
4. 在无授权前提下完成字段契约、PIT 契约、staging schema、纯校验器与测试；
5. 对 G9 / G3 分别给出 PASS / PARTIAL / FAIL / NOT_TESTED，并附证据与限制；
6. 四个原始库与 M6.4 staging 保持不变。

## 2. 非目标（硬约束）

- 不调用 iFinD / Tushare / 理杏仁 / CnOpenData / Choice / JoinQuant；
- 不使用 M6.6 剩余额度；
- 不登录、不注册、不购买、不发送工单；
- 不持久化许可未确认的真实 payload；
- 不修改四个原始库、M6.4/M6.5/M6.6 staging；
- 不训练/调参/回测优化，不解冻 Model A/B/A+B，不申请 M7；
- 不 push、不 PR、不 merge、不部署。

## 3. 继承的 Gate 状态

见 `00-m67-inherited-gate-matrix.csv`。摘要：

| Gate | M6.6 | M6.7 默认结果（无新数据） |
| --- | --- | --- |
| G1 | PASS（仅 staging） | 保持 staging PASS，不解锁研究库 |
| G2 | PARTIAL | 保持 PARTIAL |
| G3 | FAIL | FAIL / NOT_TESTED |
| G9-PRICE | PARTIAL | PARTIAL |
| G9-CA | FAIL | FAIL |
| G9-PIT | FAIL | PIT_UNVERIFIED |
| G9-LICENSE | FAIL | `NOT_ESTABLISHED` |
| G9 总 | FAIL | FAIL |

## 4. 本阶段允许写入

- `reports/trend_engine/m67/`
- `scripts/m67_*.py`
- `tests/test_trend_m67_*.py`
- `backend/data/m67_sandbox/`（仅离线派生产物，不含许可未确认的原始数据）

## 5. 执行顺序

N0 基线 → N1 证据对账 → N2 调用链审计 → N3 G9 契约 → N4 G3 契约 → N5 PIT/血缘 → N6 供应商矩阵与工单 v2 → N7 授权守卫与 fake provider → N8 全量验证 → N9 本地提交。

## 6. 阶段成功标准

不是让 G9/G3「看起来通过」，而是准确回答：**缺什么证据、谁能提供、什么许可允许使用、如何验证、证据不足时如何安全保持冻结。**
