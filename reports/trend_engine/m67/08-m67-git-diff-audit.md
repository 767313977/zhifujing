# M6.7 N8：Git 差异审计（提交前）

- 日期：2026-11-16
- 分支：`feat/trend-engine-m67-data-gap-closure`
- 基线 HEAD：`b310d630dcc13ea0fa46b197f4b67c28d27708d0`

---

## 1. 工作区清单

| 状态 | 路径 | 说明 |
| --- | --- | --- |
| M | `reports/s6/s62/migration-report.json` | 既有测试副作用，**不提交** |
| ?? | `backend/app/services/trend_engine/authz.py` | 授权守卫（新增） |
| ?? | `backend/app/services/trend_engine/data_contracts.py` | 数据契约（新增） |
| ?? | `reports/trend_engine/m66/08-m66-branch-push-acceptance.md` | M6.6 遗留，**本阶段不自动提交** |
| ?? | `reports/trend_engine/m67/` | 本阶段全部报告与 CSV |
| ?? | `scripts/m67_build_ifind_v2.py` | 工单 v2 生成器 |
| ?? | `scripts/m67_freeze_baseline.py` | 哈希冻结 |
| ?? | `tests/test_trend_m67_g9_contract.py` | G9 契约测试 |
| ?? | `tests/test_trend_m67_g3_contract.py` | G3 契约测试 |
| ?? | `tests/test_trend_m67_authorization_guard.py` | 授权守卫测试 |

## 2. 禁止项核对

| 检查 | 结果 |
| --- | --- |
| 是否包含 `backend/data/**` | 否 |
| 是否包含 `*.db` / `*.db-wal` / `*.db-shm` | 否 |
| 是否包含 `.env` / 密钥 | 否 |
| 是否包含真实供应商 payload | 否 |
| 是否修改 M6.4/M6.5/M6.6 报告 | 否（M6.6 推送报告只是未跟踪遗留，未被修改） |
| 是否修改生产代码行为 | 否（仅新增两个独立模块 + 脚本 + 测试） |

## 3. 提交范围建议

```text
backend/app/services/trend_engine/authz.py
backend/app/services/trend_engine/data_contracts.py
scripts/m67_build_ifind_v2.py
scripts/m67_freeze_baseline.py
tests/test_trend_m67_g9_contract.py
tests/test_trend_m67_g3_contract.py
tests/test_trend_m67_authorization_guard.py
reports/trend_engine/m67/**
```

不提交：`migration-report.json`、`reports/trend_engine/m66/08-m66-branch-push-acceptance.md`。

## 4. 确定性

- 报告与 CSV 均由固定输入生成，无时间戳参与内容判断（仅记录生成时间）；
- `data_contracts` 与 `authz` 为纯函数，同输入同输出（测试已断言）。
