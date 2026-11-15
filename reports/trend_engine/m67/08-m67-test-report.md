# M6.7 N8：测试报告

- 日期：2026-11-16
- 分支：`feat/trend-engine-m67-data-gap-closure`

## 1. 定向测试

| 命令 | 结果 |
| --- | --- |
| `pytest tests/test_trend_m67_g9_contract.py tests/test_trend_m67_g3_contract.py tests/test_trend_m67_authorization_guard.py -q` | **45 passed**（0.14s） |
| M6.5 回归（provider/staging/guard） | 28 passed |
| M6.6 回归（budget/ledger/replies/g9） | 41 passed |

## 2. 全量后端

`python -m pytest -p no:cacheprovider -q` → **2018 passed, 4 skipped**（391.04s，退出码 0）

## 3. 静态检查

| 项 | 结果 |
| --- | --- |
| Ruff（backend + scripts + tests） | All checks passed |
| mypy | `NOT_RUN`（未安装） |
| `git diff --check` | 无空白错误（提交前复核） |

## 4. 失败与不稳定

| 项 | 说明 |
| --- | --- |
| 过程中出现 1 次测试失败 | `test_true_historical_snapshot_is_eligible`：fixture 的 `available_at` 晚于 `snapshot_date`，属于**测试数据错误**，修正 fixture 后通过；未放宽任何规则 |
| 既有失败 | 未发现 |

## 5. 结论

新增契约与守卫均通过；未修改 M6.5/M6.6 护栏；未跳过任何失败测试。
