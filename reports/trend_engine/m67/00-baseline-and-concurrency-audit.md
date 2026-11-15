# M6.7 N0：基线、并发与保护对象审计

- 日期：2026-11-16
- 仓库：`C:/Users/76731/Documents/trae_projects/fupan`
- 基线分支：`feat/trend-engine-m66-provider-validation`
- 基线 HEAD：`b310d630dcc13ea0fa46b197f4b67c28d27708d0`（与任务书一致）
- 新工作分支：`feat/trend-engine-m67-data-gap-closure`（自上述 HEAD 创建，未 cherry-pick / rebase）
- 证据：`00-protected-hashes-before.json`

---

## 1. 只读核对结果

| 检查 | 实际值 |
| --- | --- |
| `git rev-parse --show-toplevel` | `C:/Users/76731/Documents/trae_projects/fupan` |
| 当前分支（开始时） | `feat/trend-engine-m66-provider-validation` |
| HEAD | `b310d630dcc13ea0fa46b197f4b67c28d27708d0` |
| 工作区 | `M reports/s6/s62/migration-report.json`、`?? reports/trend_engine/m66/08-m66-branch-push-acceptance.md` |
| remote `origin` | `git@github.com:767313977/zhifujing.git`（只读，未修改） |
| 其他 remote | `zhaohuibin7`（存在但未使用） |
| M6.6 目标分支是否已存在 | 否（新建） |

## 2. 遗留文件处理

| 文件 | 状态 | 处置 |
| --- | --- | --- |
| `reports/s6/s62/migration-report.json` | 已修改（既有测试副作用） | 不暂存、不提交、不还原、不删除、不覆盖 |
| `reports/trend_engine/m66/08-m66-branch-push-acceptance.md` | 未跟踪（M6.6 推送验收报告，故意未提交） | 本阶段不自动提交；若用户要归档，另行授权 |

## 3. 并发检查（发现一个后台测试进程）

- 工作树：主工作树 + 两个历史临时工作树（`%TEMP%\s6_8_versions\new`、`old`，detached，与 M6.7 无关）。
- **发现一个非本仓库 venv 的后台进程**：

| 项 | 值 |
| --- | --- |
| PID | 7708 |
| 可执行文件 | `C:\Users\76731\AppData\Local\Programs\Python\Python314\python.exe` |
| 命令行 | `-m unittest discover -s tests` |
| 启动时间 | 2026-11-16 01:03:53 |

判定与处置：

1. 该进程是**另一个测试运行器**，不是本阶段启动的（本阶段统一使用 `.venv\Scripts\python.exe`）。
2. 风险：它可能写入测试副作用文件（如 `reports/s6/s62/migration-report.json`），但不应写入四个受保护库或 M6.4 staging。
3. 处置：**不终止该进程**（终止属于对他人运行的破坏性操作）；本阶段对所有写操作使用独立新文件；阶段结束时重新核对四个库与 M6.4 staging 哈希；若发现未解释变化立即停止。
4. 未检测到 `git index.lock`。

## 4. 四个受保护数据库（阶段开始）

| 库 | SHA256 | 与 M6.6 基线 |
| --- | --- | --- |
| `b1_research.db` | `A93BC080EB9E366D08025C0A247E9AF525674A0294EC4D6012B7DBFD8941C56B` | 一致 |
| `history.db` | `9267652175594871B49456253BB0437CD5738F0D19257AB8CCA961BA5F921A6A` | 一致 |
| `b2_final_snapshot.db` | `371512BC81F00A604788B02AF7D4BE9467C75F28CDFB984F0187DD81001520D3` | 一致 |
| `fupan.db` | `B65D59A65AAEE286F65B91AEE70DF75FF9B34E83E6C266E69BE4C491996337EE` | 一致 |

## 5. M6.4 staging

- 16/16 文件哈希一致（14 个证据文件 + 2 个 SQLite 边车：`-shm` 32,768 字节、`-wal` 0 字节）。
- 本阶段只做**字节级读取**计算哈希；若需查询 SQLite，使用 `immutable=1` 只读 URI，避免新增边车文件。

## 6. 写入边界

本阶段只允许写：

- `reports/trend_engine/m67/`
- `scripts/m67_*.py`
- `tests/test_trend_m67_*.py`
- `backend/data/m67_sandbox/`（仅离线派生测试产物）

不写：四个原始库、M6.4/M6.5/M6.6 staging、生产配置、调度。

## 7. N0 门禁

| 门禁 | 结果 |
| --- | --- |
| 基线 HEAD 与任务书一致 | 通过 |
| 新分支自基线创建 | 通过 |
| 遗留文件状态已核实且未处置 | 通过 |
| 并发进程已识别并有处置策略 | 通过（后台 unittest 进程，仅监控） |
| 四个库 SHA 记录 | 通过 |
| M6.4 staging 16/16 | 通过 |
| 是否需要停止 | 否（纯文档/离线工作可继续；涉及该进程可能写入的文件时先复核） |
