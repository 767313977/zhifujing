# M6.4 Batch 0：基线、SDK 环境与安全

- 分支：`feat/trend-engine-m64-ifind-data-ingest`（起点 `443c7d2`）
- 环境：Windows；Python 3.13.13；SQLite 3.50.4
- 结论：**Git 基线一致；本机未安装 `iFinDPy` SDK；改用仓库既有 iFinD HTTP MCP 客户端完成受控最小样本；原始库全程只读。**

---

## 1. Git 与工作区

| 项 | 值 |
| --- | --- |
| M6.3 HEAD（本地/远端） | `443c7d2` / `443c7d2`，差异 0/0 |
| 新分支 | `feat/trend-engine-m64-ifind-data-ingest` |
| 工作区 | 仅既有 `reports/s6/s62/migration-report.json`（未暂存/未提交） |
| 历史提交 | 未重写 |

## 2. SDK 环境

| 检查 | 结果 |
| --- | --- |
| `import iFinDPy` | **不可用（NOT_INSTALLED）** |
| `.venv` 内 iFinD 包 | 无 |
| 实际传输 | 项目既有 `backend/app/sources/ifind.py`（JSON-RPC over HTTPS，MCP） |
| 认证 | `IFIND_AUTH_TOKEN` 只读自本地 `.env`；**未输出、未写入报告** |
| 出站策略 | `deploy/s6/egress-policy.json` 已允许 `51ifind.com:8643` |
| 并发 | 串行调用；未并发、未多 worker |
| 多设备 | 未使用 SDK 登录；HTTP 会话为无状态，无法确认其他设备会话 → 记录 `SESSION_UNVERIFIED` |

## 3. 用量基线

| 项 | 值 |
| --- | --- |
| 官方用量统计 | **`USAGE_UNVERIFIED`**（无 SDK，无法调用 `THS_DataStatistics`） |
| 本地调用计数 | 19 次（探针 5 + 样本 12 + 因子复核 2） |
| 估算返回单元格 | 约 2,230 |
| 本轮预算 | 50,000 单元格；已用 ≈4.5% |
| 月度配额约束 | 仓库配置记录账号按**调用次数**计量（本周期约 5,452 次），本轮 19 次 |

## 4. 数据库只读

| 检查 | 结果 |
| --- | --- |
| `b1_research.db` SHA256 | `A93BC080…C56B`（任务前后一致） |
| `history.db` / `b2_final_snapshot.db` / `fupan.db` | 未打开写入，SHA 不变 |
| 仅读方式 | `mode=ro` + `PRAGMA query_only=ON` |
| 用量表写入 | 探针内将 `record_call` 置空，**未写 `fupan.db`/`b1` 的 `ifind_usage`** |

## 5. 允许写入

- `backend/data/m64_staging/batch_20261115T152701Z/`（新建，创建前不存在）
- `reports/trend_engine/m64/`
- `scripts/m64_*.py`、`tests/test_trend_m64_*.py`

## 6. 本轮暂停点

- 未登录 SDK → 不尝试猜密码；
- 无官方用量统计 → 只做最小样本，不做全量补采；
- 复权字段定义不清 → 不构造 adjusted series（见 `02`）。
