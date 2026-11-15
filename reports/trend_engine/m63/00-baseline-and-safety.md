# M6.3 Batch 0：基线、分支与数据安全

- 执行时间：2026-11-15
- 环境：Windows PowerShell；Python 3.13.13（`.venv`）；SQLite 3.50.4
- 结论：**基线一致、工作区无未识别变更、数据库身份明确、只读约束已实证。允许进入 Batch 1。**

---

## 1. Git 基线

| 项 | 值 |
| --- | --- |
| 起点分支 | `feat/trend-engine-m62-data-integrity` |
| M6.2 HEAD（本地） | `abb70f834070d94be3d7f78c077ec1bb5cc788c2` |
| M6.2 HEAD（远端） | `abb70f834070d94be3d7f78c077ec1bb5cc788c2` |
| 本地/远端差异 | `0 0` |
| 新分支 | `feat/trend-engine-m63-core-data-fix`（从 `abb70f8` 切出） |
| 当前 HEAD | `abb70f8` |
| `master` | 未修改、未合并、未推送 |

历史提交未重写：仅执行 `git checkout -b`，未 rebase / reset / amend / force-push。

### 1.1 工作区

```
 M reports/s6/s62/migration-report.json
```

| 路径 | 归属 | 处理 |
| --- | --- | --- |
| `reports/s6/s62/migration-report.json` | M6.2 之前既有改动，非本轮产生 | 未触碰；diff 哈希 `6caebef86ec420cb47df242564e617e1dfecad70`，与本轮开始时一致 |

---

## 2. 数据库身份

只读扫描 + SHA256（探针：`reports/trend_engine/m63/sandbox_baseline_readonly_probe.json`）。

| 数据库 | 字节数 | SHA256 |
| --- | ---: | --- |
| `backend/data/b1_research.db` | 1,441,378,304 | `A93BC080EB9E366D08025C0A247E9AF525674A0294EC4D6012B7DBFD8941C56B` |
| `backend/data/history.db` | 869,801,984 | `9267652175594871B49456253BB0437CD5738F0D19257AB8CCA961BA5F921A6A` |
| `backend/data/b2_final_snapshot.db` | 1,441,378,304 | `371512BC81F00A604788B02AF7D4BE9467C75F28CDFB984F0187DD81001520D3` |
| `backend/data/fupan.db` | 513,519,616 | `B65D59A65AAEE286F65B91AEE70DF75FF9B34E83E6C266E69BE4C491996337EE` |

---

## 3. 只读证据

| 语句 | 四个库的结果 |
| --- | --- |
| `CREATE TABLE` / `INSERT` / `UPDATE` / `DELETE` / `VACUUM` / `REINDEX` | 全部拒绝：`attempt to write a readonly database` |
| `DROP TABLE IF EXISTS _m62_probe` | 空操作（对象不存在），无副作用 |
| `PRAGMA query_only` | 回读 = 1 |
| 打开前后 SHA256 | 完全一致 |
| 状态 | 四个库均 `READONLY_PROVEN`，`all_proven: true` |

---

## 4. 本轮允许写入的路径

| 路径 | 用途 | 约束 |
| --- | --- | --- |
| `backend/data/m63_sandbox/` | 独立研究副本 / 派生数据库 | 创建前必须证明目标文件不存在；不得覆盖 |
| `reports/trend_engine/m63/**` | 报告与 CSV/JSON 证据 | 只新增，不覆盖 M6.1/M6.2 报告 |
| `scripts/m63_*.py` | 审计、派生、验证脚本 | 只读原库 |
| `tests/test_trend_m63_*.py` | 新增测试 | — |

明确禁止：写 b1 / history / b2_final / fupan / 生产库；改 M6.1、M6.2 历史报告数字；部署、合并 master、修改生产调度。

`backend/data/m63_sandbox/` 创建前已确认不存在（`Test-Path` 为假），创建后为空目录。

---

## 5. Batch 0 门禁

| 门禁 | 结果 |
| --- | --- |
| M6.2 本地/远端一致 | 通过 |
| M6.1/M6.2 历史未重写 | 通过 |
| 工作区仅既有改动 | 通过 |
| 数据库身份明确 | 通过 |
| 只读可证明 | 通过 |
| 生产隔离 | 通过 |

**Batch 0 通过。**
