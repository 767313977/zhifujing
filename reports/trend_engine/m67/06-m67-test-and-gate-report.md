# M6.7 测试与 Gate 报告

- 日期：2026-11-16
- 新增测试：45 项（三个文件）
- 全量后端：**2018 passed, 4 skipped**（391.04 秒）
- Ruff：All checks passed
- mypy：`NOT_RUN`（未安装）

---

## 1. 新增测试清单

| 文件 | 项数 | 覆盖 |
| --- | ---: | --- |
| `tests/test_trend_m67_g9_contract.py` | 18 | 空值六态、adjusted 类型、因子语义、事件字段、时间顺序、PIT、质量状态、确定性、血缘 |
| `tests/test_trend_m67_g3_contract.py` | 10 | 当前快照、日期参数、可得时间、历史资格、SNAPSHOT_ONLY、回填、映射审批、taxonomy |
| `tests/test_trend_m67_authorization_guard.py` | 15 | 11 类拒绝、用途分离、FakeProvider 零网络、汇总计数 |

## 2. 测试矩阵对照（任务书第 10 章）

| 类别 | 覆盖 | 结果 |
| --- | --- | --- |
| 价格域 | raw/adjusted 不混列；缺 adjust_type 拒绝 | PASS |
| 复权因子 | 方向/基准日/累计规则缺失不通过 | PASS |
| 公司行为 | NULL≠0；未决不分类 | PASS |
| PIT | available_at 缺失不可严格 OOS；当前快照不伪装历史 | PASS |
| G3 | 当前/历史分离；in/out 边界 | PASS |
| 许可 | 未授权 provider/dataset/purpose/storage 拒绝 | PASS |
| 请求预算 | 未授权时网络数 0 | PASS |
| staging | 唯一目录、拒绝覆盖、不写四库 | PASS（M6.5 回归） |
| 血缘 | 缺项降级 | PASS |
| 确定性 | 同输入同输出 | PASS |
| 安全 | 不记录 Token/账号 | PASS |

## 3. 回归

M6.5 的 28 项与 M6.6 的 41 项沿用，未修改任何既有护栏。
