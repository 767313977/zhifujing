# M6.6 增补 Batch 0：iFinD 个人基础版 100 次请求基线

- 日期：2026-11-16
- 分支：`feat/trend-engine-m66-provider-validation`
- 起点 HEAD：`7b7889d`（M6.5 末次提交，本地与远端一致）
- 批次目录：`backend/data/m66_staging/ifind_personal_100_20261115T162631Z/`
- 预算账本：`request_budget_ledger.jsonl`（初始 `used=0, limit=100`）
- 原始 payload：**未持久化**（`RAW_PAYLOAD_PERSISTENCE_NOT_AUTHORIZED`），只保存结构摘要、哈希与派生指标

---

## 1. 仓库与并发

| 检查 | 结果 |
| --- | --- |
| 当前分支 | `feat/trend-engine-m66-provider-validation` |
| HEAD | `7b7889d17f0aff87e2de2ccde86f54b78f481e13` |
| 工作区 | 仅 `reports/s6/s62/migration-report.json`（既有测试副作用，未提交） |
| 并发进程 | 无 pytest/python 在跑；无 `index.lock` |
| 工作树 | 主工作树 + 两个历史临时工作树（`%TEMP%\s6_8_versions\*`，detached，未触碰） |

## 2. 四个受保护数据库（调用前）

| 库 | SHA256 |
| --- | --- |
| `b1_research.db` | `A93BC080EB9E366D08025C0A247E9AF525674A0294EC4D6012B7DBFD8941C56B` |
| `history.db` | `9267652175594871B49456253BB0437CD5738F0D19257AB8CCA961BA5F921A6A` |
| `b2_final_snapshot.db` | `371512BC81F00A604788B02AF7D4BE9467C75F28CDFB984F0187DD81001520D3` |
| `fupan.db` | `B65D59A65AAEE286F65B91AEE70DF75FF9B34E83E6C266E69BE4C491996337EE` |

与 M6.5 基线一致。M6.4 staging 16/16 文件（14 个证据文件 + 2 个 0 字节/32KB SQLite 边车）哈希与 M6.6 基线一致，调用后复核见 `sha_check.json`。

## 3. iFinD 账号与额度

| 项 | 值 |
| --- | --- |
| 账号类型 | 用户现有个人基础版 |
| 传输方式 | 项目既有 HTTP MCP 客户端（`backend/app/sources/ifind.py`），`iFinDPy` **未安装** |
| Token | `.env` 中存在 `IFIND_AUTH_TOKEN`；**值未读取、未打印、未写入任何文件** |
| 额度来源 | 用户截图：剩余 2,940 / 总计 5,000，计量周期 2026-10-17~2026-11-17 |
| 实时余额 | **UNVERIFIED**（账号无官方用量 API，`tools/list` 也无 usage 工具） |
| 计量口径 | 本项目按「每次实际 HTTP 请求计 1 次」保守计数；是否与供应商计量一致 **UNVERIFIED** |

处置：本轮计划最多 100 次，实际使用 42 次；截图剩余额度约为计划的 29 倍，且每条响应都检查配额耗尽提示，一旦出现立即停止。该不确定性已在本报告与最终报告中标注，不把「100 次请求」表述为「100 计量单位」。

## 4. 预算分配与实际

| 用途 | 上限 | 实际 |
| --- | ---: | ---: |
| A. 能力清单与接口确认 | 8 | 6 |
| B. G9 价格与公司行为 | 52 | 30（含 3 次 metrics 复算） |
| C. G3 历史成分 | 30 | 6（含 2 次客户端指向错误 server 的失败请求） |
| D. 重试与保留 | 10 | 0 |
| **合计** | **100** | **42** |

## 5. 安全措施

- 请求前预占预算，失败/超时/重试均计数（`RequestBudgetGuard`，13 项测试）；
- `record_call` 在本进程置空，**不写任何数据库**；
- 不下载供应商提供的 CSV 全量链接（`o.thsi.cn`），避免绕过预算；
- 未持久化原始 payload；报告只含结构、字段状态、派生指标与哈希。

## 6. Batch 0 门禁

| 门禁 | 结果 |
| --- | --- |
| 分支/HEAD/工作区符合预期 | 通过 |
| 无并发写入者 | 通过 |
| 四个库 SHA 记录且未变 | 通过 |
| M6.4 staging 未变 | 通过（16/16） |
| 预算守卫可用 | 通过（13 项测试） |
| 额度可核实 | **部分**：无 API，仅有截图；已按保守计数并在响应内监控配额提示 |
