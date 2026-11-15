# M6.2 Batch 0：基线与安全核验

- 执行时间：2026-11-15（Asia/Shanghai）
- 执行环境：Windows PowerShell；`.venv\Scripts\python.exe`（3.13.13）；SQLite 3.50.4
- 结论：**基线一致、工作区无未识别改动、四个数据库身份可核对、只读约束已实证、生产零触碰。Batch 0 通过，允许进入 Batch 1。**

---

## 1. Git 身份

| 项 | 值 |
| --- | --- |
| 仓库 | `c:\Users\76731\Documents\trae_projects\fupan` |
| 新分支 | `feat/trend-engine-m62-data-integrity` |
| 分支起点 | `782a5f301bb9c7aeef62ed77408c8777c13593cc` |
| 本地 `feat/trend-engine-m61` | `782a5f301bb9c7aeef62ed77408c8777c13593cc` |
| 远端 `origin/feat/trend-engine-m61` | `782a5f301bb9c7aeef62ed77408c8777c13593cc` |
| m61 本地/远端差异 | `0 0`（无领先、无落后） |
| 本地 `feat/trend-engine-v1` | `32223ea137c659d6ee243140e690a54bba50f936` |
| 远端 `origin/feat/trend-engine-v1` | `32223ea137c659d6ee243140e690a54bba50f936` |
| v1 本地/远端差异 | `0 0` |
| `master` | 未修改、未合并、未推送 |

### 1.1 提交链（`git log --oneline`）

```
782a5f3 docs(m61): record batch commit hashes
21f92b8 feat(m61): add trade-level risk exits and portfolio study
7ed3ae6 feat(m61): add redesigned variants and purged walk-forward validation
98bb217 feat(m61): add model failure attribution and MFE support
6b84341 feat(m61): add baseline audit and data quality governance
32223ea docs(trend-engine): record M1-M6 acceptance and push history
dbf7dd2 feat(trend-engine): add M6 unified backtest and validation reports
8dfe433 feat(trend-engine): add trend risk engine
469ede9 fix(research): unify sector percentile and add trend data gates
a6a5295 feat(trend-engine): add Model B trend continuation
```

### 1.2 基线对象存在性核对

| 对象 | 期望 | 实际 |
| --- | --- | --- |
| M6 `dbf7dd2` | 存在 | 存在 |
| M6.1 起点 `32223ea` | 是 HEAD 祖先 | `git merge-base --is-ancestor` 退出码 0 |
| `6b84341` / `98bb217` / `7ed3ae6` / `21f92b8` / `782a5f3` | 均存在 | 均存在 |

### 1.3 分支关系

```
feat/trend-engine-v1        32223ea  (= M6 dbf7dd2 + M1–M6 验收文档)
       │
       └── feat/trend-engine-m61   6b84341 → 98bb217 → 7ed3ae6 → 21f92b8 → 782a5f3
                                          │
                                          └── feat/trend-engine-m62-data-integrity  （本轮，起点 782a5f3）
```

M0～M6.1 历史未被重写：本轮只做 `git checkout -b`，未执行 rebase / reset / force-push / amend。

---

## 2. 工作区变更清单

`git status --short` 输出（创建分支后、写入 Batch 0 产物前）：

```
 M reports/s6/s62/migration-report.json
```

| 路径 | 状态 | 归属 | 处理 |
| --- | --- | --- | --- |
| `reports/s6/s62/migration-report.json` | 已修改未提交 | **M6.2 之前既有改动**，非本批次产生 | **未触碰、未暂存、未提交**；diff 内容哈希 `6caebef86ec420cb47df242564e617e1dfecad70` 已记录，收尾时复核 |

除上述单一文件外，工作区无其他已跟踪改动；新增文件仅本批次产物（见第 6 节）。

---

## 3. 数据库身份

路径均相对仓库根目录，文件在 Batch 0 前后均未变化。

| 数据库 | 字节数 | SHA256 |
| --- | ---: | --- |
| `backend/data/b1_research.db` | 1,441,378,304 | `A93BC080EB9E366D08025C0A247E9AF525674A0294EC4D6012B7DBFD8941C56B` |
| `backend/data/history.db` | 869,801,984 | `9267652175594871B49456253BB0437CD5738F0D19257AB8CCA961BA5F921A6A` |
| `backend/data/b2_final_snapshot.db` | 1,441,378,304 | `371512BC81F00A604788B02AF7D4BE9467C75F28CDFB984F0187DD81001520D3` |
| `backend/data/fupan.db` | 513,519,616 | `B65D59A65AAEE286F65B91AEE70DF75FF9B34E83E6C266E69BE4C491996337EE` |

同目录下另有 `b0_trial.db`、`b0_trial_before_rollback.db`、`b0_trial_before_rps.db`、`forward_research.db`（历史实验残留）。本轮**未打开、未修改**，也不作为研究输入。

`b1_research.db` 与 `b2_final_snapshot.db` 字节数相同但 SHA256 不同，属不同快照，**不可当作同一数据库互换使用**。

---

## 4. 只读约束实证

探针脚本：`scripts/m62_readonly_probe.py`；输出：`reports/trend_engine/m62/readonly_probe.json`。

执行：

```
.venv\Scripts\python.exe scripts\m62_readonly_probe.py \
  --db backend/data/b1_research.db --db backend/data/history.db \
  --db backend/data/b2_final_snapshot.db --db backend/data/fupan.db --hash \
  --out reports/trend_engine/m62/readonly_probe.json
```

退出码：`0`；`all_proven: true`。

对每个数据库：

1. 以 `file:<abs_path>?mode=ro` URI 打开（文件不存在即失败，不会新建）；
2. `PRAGMA query_only=ON` 后回读得到 `1`；
3. 逐条尝试写语句；
4. 关闭后复核字节数、mtime、SHA256。

| 语句 | b1 | history | b2_final | fupan |
| --- | --- | --- | --- | --- |
| `CREATE TABLE` | 拒绝：attempt to write a readonly database | 同 | 同 | 同 |
| `INSERT` | 拒绝 | 拒绝 | 拒绝 | 拒绝 |
| `UPDATE` | 拒绝 | 拒绝 | 拒绝 | 拒绝 |
| `DELETE` | 拒绝 | 拒绝 | 拒绝 | 拒绝 |
| `DROP TABLE IF EXISTS _m62_probe` | 空操作（表不存在，未创建成功，无副作用） | 同 | 同 | 同 |
| `VACUUM` | 拒绝 | 拒绝 | 拒绝 | 拒绝 |
| `REINDEX` | 拒绝 | 拒绝 | 拒绝 | 拒绝 |
| 打开前后 SHA256 | 一致 | 一致 | 一致 | 一致 |
| 状态 | `READONLY_PROVEN` | `READONLY_PROVEN` | `READONLY_PROVEN` | `READONLY_PROVEN` |

说明：`DROP TABLE IF EXISTS` 在目标表从未创建时是合法空操作，SQLite 不报错；该项不构成写入成功的证据，探针单独标记为 `noop`，其余六项均为硬失败。

本仓库既有研究脚本（`scripts/audit_trend_data_m61.py` 等）同样只使用 `mode=ro` + `PRAGMA query_only=ON`；本轮复用同一模式，不新增写入路径。

---

## 5. 生产隔离

本轮未执行、也不会执行：

| 禁止项 | 状态 |
| --- | --- |
| 连接生产服务器 / 生产数据库 | 未连接；无生产凭据、无远程连接串 |
| 访问或修改生产库 | 未访问；仅打开上表 4 个本地文件 |
| 部署 / 发布 | 未执行 |
| 合并 master / 推送 master | 未执行 |
| 修改生产配置与调度 | 未执行 |
| 安装 `data_revision` | 未安装，未修改依赖清单 |
| 数据库写操作（INSERT/UPDATE/DELETE/CREATE/ALTER/DROP/VACUUM/REINDEX） | 已实证被拒绝 |
| 新建 / 覆盖 / 重命名 / 删除数据库 | 未执行；未创建任何 `.db` 文件 |
| 覆盖 `reports/s6/s62/migration-report.json` | 未触碰 |
| rebase / reset / force-push / 改写历史 | 未执行 |

---

## 6. 本轮写入白名单

仅允许在以下位置产生新文件或修改文件：

| 路径 | 用途 |
| --- | --- |
| `reports/trend_engine/m62/**` | 本批次全部报告与证据 JSON/CSV |
| `scripts/m62_*.py` | 审计、对账、复现脚本 |
| `tests/test_trend_m62_*.py` | 新增测试 |
| `backend/data/m62_cache/**` | 中间特征缓存（gitignore，不提交、不入库） |
| `docs/trend_engine/**` | 如需补充说明（优先不改） |

明确禁止：任何 `.db` 文件、任何数据库内对象、`backend/app/**` 生产逻辑（除非 Batch 4 证明存在最小必要修复并单独提交）、生产配置与调度文件。

---

## 7. Batch 0 门禁判定

| 门禁 | 结果 |
| --- | --- |
| 远端与本地 M6.1 一致 | 通过（0/0，同一 SHA） |
| M0～M6.1 提交完整、未重写 | 通过 |
| 工作区仅有既知改动 | 通过（仅 `migration-report.json`） |
| 数据库身份明确 | 通过（4 个库字节数 + SHA256 记录） |
| 只读连接 + `query_only` 可证明 | 通过（7 条语句中 6 条硬拒绝，1 条空操作；文件哈希不变） |
| 生产隔离 | 通过（未连接、未部署、未写入） |

**Batch 0 通过。** 遗留说明：`b0_*` 与 `forward_research.db` 属历史实验残留，本轮不纳入研究输入，也不做删除或重命名。
