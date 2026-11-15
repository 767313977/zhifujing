# M6.6 Batch 0：基线冻结、并发检查与架构审计

- 日期：2026-11-16
- 任务书：`Trend_Engine_M6.6_TRAE_Execution_Taskbook_v1.0.md`
- 证据：`reports/trend_engine/m66/baseline_freeze.json`
- 脚本：`scripts/m66_freeze_baseline.py`（只读）
- 网络调用：**0 次**；原始库写入：**0 次**

---

## 1. 基线核对结论

| 项 | 用户提供 / M6.5 记录 | 现场实测 | 结论 |
| --- | --- | --- | --- |
| 仓库路径 | `c:\Users\76731\Documents\trae_projects\fupan` | 同 | 一致 |
| 起点分支 | `feat/trend-engine-m65-multisource-data` | 同 | 一致 |
| 起点 HEAD | `7b7889d` | `7b7889d17f0aff87e2de2ccde86f54b78f481e13` | 一致 |
| M6.5 远端 | 同名分支 | `origin/feat/trend-engine-m65-multisource-data` | 一致 |
| 远端是否领先/落后 | — | 本地与远端同为 `7b7889d` | 无差异 |
| 新分支 | — | `feat/trend-engine-m66-provider-validation`（自 `7b7889d` 创建） | 已创建 |

`git log -8 --oneline --decorate` 显示 M6.5 的 8 个批次提交完整，末次为 `7b7889d docs(m65): publish approval matrix and final acceptance`。

## 2. 工作区与并发检查

| 检查 | 结果 |
| --- | --- |
| `git status --short` | `M reports/s6/s62/migration-report.json`（后端全量 pytest 的既有副作用，未纳入提交） |
| 是否存在 `index.lock` | 否 |
| 是否有 pytest / python 进程在跑 | 否（仅 Trae IDE 自身进程） |
| `git worktree list` | 主工作树 + 两个历史临时工作树（`%TEMP%\s6_8_versions\new` / `old`，detached，与 M6.5/M6.6 无关） |
| 是否有其他执行者修改同一仓库 | 未发现 |

处理方式：`migration-report.json` **不覆盖、不还原、不提交**，仅记录其存在。两个临时工作树只读登记，不清理（清理属于破坏性操作）。

## 3. 四个受保护数据库

| 库 | SHA256 | 与 M6.5 基线 |
| --- | --- | --- |
| `b1_research.db` | `A93BC080EB9E366D08025C0A247E9AF525674A0294EC4D6012B7DBFD8941C56B` | **一致** |
| `history.db` | `9267652175594871B49456253BB0437CD5738F0D19257AB8CCA961BA5F921A6A` | **一致** |
| `b2_final_snapshot.db` | `371512BC81F00A604788B02AF7D4BE9467C75F28CDFB984F0187DD81001520D3` | **一致** |
| `fupan.db` | `B65D59A65AAEE286F65B91AEE70DF75FF9B34E83E6C266E69BE4C491996337EE` | **一致** |

## 4. M6.4 staging 保全（含一处需说明的偏差）

- M6.5 冻结的 **14 个证据文件：14/14 SHA256 完全一致**。
- 目录内新增 **2 个 SQLite 边车文件**：

| 新增文件 | 大小 | 创建时间 |
| --- | ---: | --- |
| `m64_ifind_staging.db-shm` | 32,768 | 2026-11-16 00:04:15 |
| `m64_ifind_staging.db-wal` | 0 | 2026-11-16 00:04:15 |

原因判定：M6.5 Batch 6 的只读复核脚本（`scripts/m65_g1_preservation.py`）以 `mode=ro` 打开 WAL 模式的 staging 库时，SQLite 为建立共享内存索引会创建 `-shm`，并伴生 0 字节 `-wal`。主库 `.db` 哈希未变、WAL 为 0 字节，说明**没有任何数据写入**。

处置：
1. 不删除这两个边车文件（删除属于对证据目录的写操作，且无必要）；
2. 在 M6.6 冻结清单中单列登记，不计入「14 个证据文件」；
3. M6.6 后续只读打开 staging 时改用**不可变只读 URI**（`immutable=1`），避免再次产生边车文件。

## 5. M6.5 核心文件冻结

27 个文件纳入 M6.6 基线：`backend/app/services/trend_engine/multisource.py`、3 个 `scripts/m65_*.py`、3 个 `tests/test_trend_m65_*.py`、`reports/trend_engine/m65/` 下 20 个交付物。逐文件哈希见 `baseline_freeze.json → m65_core_files`。

## 6. 架构审计

### 6.1 模块与调用关系

```
reports/trend_engine/m65/*.md|csv|json          （只读证据）
        ↑ 生成
scripts/m65_freeze_baseline.py   只读 → 冻结哈希
scripts/m65_g1_preservation.py   只读 b1 + m64 staging → 复核报告
scripts/m65_write_schema_draft.py 读模块常量 → 写 schema 草案
        ↑
backend/app/services/trend_engine/multisource.py
   ├── ProviderRegistry.get()  ── 授权闸门（默认仅 iFinD）
   ├── NotImplementedProvider  ── 四方法全部抛错
   ├── ProviderResult          ── 空结果需显式确认
   └── StagingBatch            ── 仅写 staging 批次目录
tests/test_trend_m65_{provider,staging,guard}.py  ── 28 项护栏
```

### 6.2 协议入口

| 入口 | 行为 |
| --- | --- |
| `ProviderRegistry(allowed=...)` | 默认 `{"iFinD"}`；未知/未授权/未注册一律 `ProviderNotAuthorized` |
| `CorporateActionProvider.get_corporate_actions / get_adjustment_factors` | 未接入即 `ProviderNotImplemented` |
| `SectorMembershipProvider.get_sector_snapshot / get_sector_membership_events` | 同上 |
| `StagingBatch.stage_raw / stage_corporate_action / stage_factor / stage_snapshot / stage_mapping / stage_error` | 只追加；重复 payload 触发 `IntegrityError` |
| `assert_not_research_db(path)` | 命中四个库名立即 `StagingViolation` |

### 6.3 网络调用入口（全仓库范围内与本阶段相关者）

| 入口 | 是否本轮启用 |
| --- | --- |
| `backend/app/sources/ifind.py`（HTTP MCP 客户端，Token 来自 `.env`） | **否** |
| `multisource.py` | 无网络代码（扫描 0 处网络特征行） |
| `scripts/m65_*.py` / `scripts/m66_*.py` | 无网络代码 |
| 其他 `backend/app/sources/*`（akshare/eastmoney/tencent 等） | 与本阶段无关，未调用 |

### 6.4 潜在写库入口

| 入口 | 目标 | 是否可触达四个受保护库 |
| --- | --- | --- |
| `StagingBatch.__init__` | `backend/data/m65_staging/<batch_id>/m65_multisource_staging.db` | 否（路径校验 + 黑名单） |
| `scripts/m65_write_schema_draft.py` | `reports/trend_engine/m65/m65_staging_schema.sql` | 否 |
| 其余 `m65_*.py` | 仅写 `reports/trend_engine/m65/` | 否 |

### 6.5 注册表与未授权拦截（回归实测）

| 检查 | 结果 |
| --- | --- |
| 默认授权集合仍为 `{"iFinD"}` | 通过 |
| Tushare / Lixinger / CnOpenData / Choice / JoinQuant 调用被硬拒绝 | 通过 |
| `NotImplementedProvider` 四方法全部抛错、不返回伪成功 | 通过 |
| 空结果需 `empty_confirmed=True` | 通过 |
| staging 目录存在即拒绝覆盖、边车不误写研究库 | 通过 |

回归命令与结果：`pytest tests/test_trend_m65_provider.py tests/test_trend_m65_staging.py tests/test_trend_m65_guard.py` → **28 passed in 4.52s**。

## 7. 继承的 Gate 状态

| Gate | 状态 | 本阶段处理 |
| --- | --- | --- |
| G1 指数对齐 | PASS（仅 staging） | 只读复核，不合并 |
| G2 板块日线 | PARTIAL | 不以区间统计冒充 OHLCV |
| G3 历史成分 | FAIL | 需授权真实样本 |
| G9 价格域/公司行为 | FAIL | 需书面字段定义与真实样本 |
| RPS v3 / breadth v2 | NOT_CREATED | 禁止创建 |
| Model A/B/A+B | FROZEN | 禁止训练/调参/收益实验 |

## 8. M6.6 写入范围

只允许写：

- `reports/trend_engine/m66/`
- `backend/data/m66_staging/<new_batch_id>/`（唯一目录，存在即拒绝）
- `scripts/m66_*.py`
- `tests/test_trend_m66_*.py`
- 经审计确认必要的独立适配器模块（本轮暂无新增）

## 9. 停止规则检查

| 停止条件 | 是否触发 |
| --- | --- |
| 分支/HEAD/工作区与预期不符 | 否 |
| 其他执行者修改同一文件 | 否 |
| 需要真实调用未授权供应商 | 否（本轮全部本地离线） |
| 需要购买/注册/接受条款 | 否 |
| 供应商协议禁止当前用途 | 不适用（未取数） |
| 需要写四个受保护库 | 否 |
| M6.4/M6.5 基线未解释变化 | **否**（14/14 哈希一致，仅新增 0 字节 WAL 边车，已解释） |
| 价格/事件语义未决歧义 | 存在，但属于待验证项，不阻断本地工作 |
| 数据冲突会改变收益/账本 | 否 |
| 测试失败无法定位 | 否（28 项全通过） |

结论：**基线安全，可进入 Batch 1**。
