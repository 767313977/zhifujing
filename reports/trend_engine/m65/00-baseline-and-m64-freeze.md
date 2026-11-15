# M6.5 Batch 0：基线与 M6.4 证据冻结

- 日期：2026-11-15
- 任务书：`Trend_Engine_M6.5_MultiSource_Data_Qualification_TRAE_v2.md`
- 起点分支：`feat/trend-engine-m64-ifind-data-ingest`
- 起点 HEAD：`a77e62154384e5d82331631cb0e6b4bc4d2446b4`（= `origin/feat/trend-engine-m64-ifind-data-ingest`，本地与远端一致）
- 新分支：`feat/trend-engine-m65-multisource-data`（自 `a77e621` 创建）
- 阶段性质：**数据源资格审查 / 最小样本验证方案 / 可授权接入准备**，不是模型研究，不是 M7
- 证据文件：`reports/trend_engine/m65/baseline_freeze.json`

---

## 1. 分支与工作区

| 项 | 值 |
| --- | --- |
| `git rev-parse HEAD` | `a77e62154384e5d82331631cb0e6b4bc4d2446b4` |
| `origin/feat/trend-engine-m64-ifind-data-ingest` | `a77e62154384e5d82331631cb0e6b4bc4d2446b4` |
| 工作区变更 | `reports/s6/s62/migration-report.json`（M） |

工作区变更处理：该文件由后端全量 pytest 每次重写（S6.2 迁移报告），属测试副作用，**本轮不纳入提交、不覆盖、不还原**，仅在每次提交时保持未暂存状态。

## 2. M6.4 报告冻结（22 个文件）

`reports/trend_engine/m64/` 共 22 个文件，逐文件 SHA256 记录于 `baseline_freeze.json → m64_reports`。关键文件：

| 文件 | SHA256 |
| --- | --- |
| `07-m64-final-acceptance.md` | `1CD472028205EDD60CF13D07279FBCBFE5F8F609235BBFCED4AF96F4031878C1` |
| `index_daily_staging.csv` | `2DFF56B5E4A0A3DDC219B81C1864C6AD47EB013FFCD34BA1032613353A0FF9C6` |
| `ingestion_manifest.json` | `F57C40248BCA90D59A6698D10C37E23EE4BCE5CE283AA4F13ABE4EBABCFD39B0` |
| `m64_gate_matrix.csv` | `EFCF38F8E620E83EC4AE5CC2D16E66FC84DF21B6C80C420E1471999E5F302D57` |
| `g9_sample_validation.json` | `7F76CAE04773C6A806D1D49CEA4379B0D152C130BC72409878CE43A817085E3D` |

约定：**M6.4 报告只读**。若发现内部矛盾，只在本目录新增勘误说明，不改写原文。

## 3. M6.4 staging 冻结（1 个批次 / 14 个文件）

`backend/data/m64_staging/batch_20261115T152701Z/`：

| 文件 | SHA256（前 16 位） | 字节 |
| --- | --- | ---: |
| `m64_ifind_staging.db` | `AE998D2C43B48F1B` | 131,072 |
| `g1_index_rows.json` | `6D56393B30A7E85A` | 27,151 |
| `g2_sector_rows.json` | `C938156BAE74414B` | 11,419 |
| `g9_events.json` | `59E6527DFD36C73D` | 958 |
| `g9_factor_cross_exdate.json` | `0E8036FDE8427982` | 10,787 |
| `g9_factor_rows.json` | `2D98A38A526D7BD6` | 8,359 |
| `g9_sample_validation.json` | `B3FC38B6A6C5A41F` | 1,227 |
| `index_000001SH_20260925_20261009.json` | `7249AE715A507E6A` | 6,081 |
| `probe_ledger.json` | `42D0CA068DCDC896` | 2,796 |
| `sample_call_ledger.json` | `58CC535708AB6ABE` | 2,598 |
| `sector_daily_staging.csv` | `044E341C78DEBBCC` | 5,113 |
| `stock_events_600519_2025.json` | `E2D0BEF623FB20A4` | 296 |
| `stock_history_600519_20250701_20250720.json` | `F4BE49EEEAE44A16` | 8,250 |
| `tools_list.json` | `B2E24FD950B6D287` | 6,550 |

完整哈希见 `baseline_freeze.json → m64_staging`。staging 目录本身被 `.gitignore` 排除，不进版本库。

## 4. 四个原始库 SHA256（任务前）

| 库 | SHA256 | 字节 |
| --- | --- | ---: |
| `backend/data/b1_research.db` | `A93BC080EB9E366D08025C0A247E9AF525674A0294EC4D6012B7DBFD8941C56B` | 1,441,378,304 |
| `backend/data/history.db` | `9267652175594871B49456253BB0437CD5738F0D19257AB8CCA961BA5F921A6A` | 869,801,984 |
| `backend/data/b2_final_snapshot.db` | `371512BC81F00A604788B02AF7D4BE9467C75F28CDFB984F0187DD81001520D3` | 1,441,378,304 |
| `backend/data/fupan.db` | `B65D59A65AAEE286F65B91AEE70DF75FF9B34E83E6C266E69BE4C491996337EE` | 513,519,616 |

与 M6.4 起始值逐位一致，说明 M6.4 全程未写原始库。任务结束时需再次复算并比对（见 Batch 7）。

## 5. M6.4 状态继承（只读引用，不改写）

| 闸门 | M6.4 | 继承说明 |
| --- | --- | --- |
| G1 指数对齐 | PASS（staging） | 仅 staging 通过，**不等于正式研究库解锁**；本轮只做保全与只读复核 |
| G2 板块日线 | PARTIAL | `sector_data` 只有区间涨跌幅/算术平均收盘价，编码未映射 |
| G3 历史成分 | FAIL | 当前账号无历史成分接口 |
| G9 价格域/公司行为 | FAIL | 因子方向不明；无每股派现/送转比例/公告时间 |
| RPS v3 / breadth v2 | NOT_CREATED | G9 未过，禁止构造 adjusted 序列 |
| 交易结论 | `PENDING_REVIEW` | 模型 A/B/A+B 继续冻结 |

## 6. 本轮写入范围（硬约束）

只允许写：

- `backend/data/m65_staging/<batch_id>/`（唯一批次目录，存在即拒绝覆盖）
- `reports/trend_engine/m65/`
- `scripts/m65_*.py`
- `tests/test_trend_m65_*.py`

禁止：

- 对 `b1_research.db` / `history.db` / `b2_final_snapshot.db` / `fupan.db` 做任何 DDL/DML/VACUUM/迁移/索引重建；如需读取必须 `mode=ro` + `PRAGMA query_only=ON`；
- 合并 M6.4 指数 staging 入 b1，或生成合并 DDL/DML/合并脚本/合并审批包；
- 第三方供应商注册、购买、接受条款、使用 Token、调用收费接口、批量下载；
- 模型训练/调参/策略收益实验、申请 M7、修改 A2/B2 与 M6.1~M6.4 历史实验报告。

## 7. Batch 0 门禁

| 门禁 | 结果 |
| --- | --- |
| 起点 HEAD 与远端一致 | 通过（`a77e621`） |
| 新分支已创建 | 通过 |
| M6.4 报告哈希已冻结 | 通过（22 文件） |
| M6.4 staging 哈希已冻结 | 通过（14 文件） |
| 四个原始库 SHA 已记录 | 通过 |
| 工作区变更已记录未覆盖 | 通过（`migration-report.json`） |
