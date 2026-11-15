# M6.4 总验收报告

- 日期：2026-11-15
- 分支：`feat/trend-engine-m64-ifind-data-ingest`（起点 `443c7d2`）
- 传输：项目既有 iFinD HTTP MCP 客户端（本机 **未安装 `iFinDPy`**）
- 真实调用：19 次；估算 2,230 单元格（预算 50,000，占 ≈4.5%）；官方用量 `USAGE_UNVERIFIED`
- 原始库：全程只读，SHA 未变
- **最终判定：`M64_PARTIALLY_ACCEPTED`**

---

## 1. 一句话结论

**iFinD 真实接口可用，G1 六大指数已在 staging 补齐并通过对齐校验；但 G9 因复权字段语义不清、公司行为金额与公告时间缺失而 FAIL，G3 因无历史成分接口而 FAIL。交易级结论仍 `PENDING_REVIEW`，模型仍冻结。**

---

## 2. 关键真实证据

| 项 | 结果 |
| --- | --- |
| 账号工具 | stock 10 个、index 3 个（`tools/list` 实测） |
| 个股日行情 | 可用；返回含周末空行，需按交易日历过滤 |
| 指数日行情 | 可用；6/6 指数覆盖 2026-09-18~10-09 |
| 复权因子 | 有字段；600519 在 2026-06-26 由 8.6760 → 8.8814 |
| 公式核验 | `raw × factor` 与返回的复权价相对误差 ≤ **1.89bp** |
| 字段歧义 | 前复权/后复权都返回在同名「收盘价」列，无方向标注 |
| 公司行为 | 只有登记/除权/派息日与进度；**无每股派现、送转比例、公告时间** |
| 板块成分 | 无接口 → G3 FAIL |
| 板块日线 | `sector_data` 只返回区间涨跌幅与算术平均收盘价，编码未映射 |

---

## 3. 闸门

| 闸门 | M6.3 | M6.4 | 说明 |
| --- | --- | --- | --- |
| G1 指数对齐 | FAIL | **PASS（staging）** | 132 行，6/6，缺 0 天；未合并 b1 |
| G2 板块日线 | FAIL | **PARTIAL** | 仅 2 个样本 |
| G3 历史成分 | FAIL | **FAIL** | 无接口 |
| G9 双价格域/公司行为 | FAIL | **FAIL** | 因子字段歧义 + 无金额/公告 |
| RPS v3 | — | **NOT_CREATED** | G9 未过，禁止构造 adjusted |
| breadth v2 | — | **NOT_CREATED** | ST 缺失 |
| 生产隔离 / 原库 SHA | PASS | **PASS** | 四个库未变 |

---

## 4. 数据安全与配额

| 检查 | 结果 |
| --- | --- |
| `b1/history/b2_final/fupan` SHA256 | 前后完全一致 |
| 写入范围 | 仅 `backend/data/m64_staging/batch_20261115T152701Z/` |
| `ifind_usage` 表 | 未写入（探针内 `record_call` 置空） |
| Token/账号 | 未输出、未写报告、未提交 |
| 并发 | 全串行；未压 QPS |
| 预算 | 19 次调用、≈2,230 单元格；未触发 80%/100% 暂停点 |

---

## 5. 交付物

`reports/trend_engine/m64/`：`00`～`07`、`ifind_function_capability.csv`、`ifind_field_dictionary.csv`、`g9_sample_validation.json`、`factor_check.json`、`factor_formula_reconciliation.csv`、`g1_validation.json`、`g2_g3_validation.json`、`index_daily_staging.csv`、`ingestion_manifest.json`、`api_usage_before_after.json`、`m64_gate_matrix.csv`。

原始样本：`backend/data/m64_staging/batch_20261115T152701Z/`（不提交 Git）。

---

## 6. 测试

| 项 | 结果 |
| --- | --- |
| 新增护栏测试 | `tests/test_trend_m64_guard.py` 8 项通过 |
| 全量后端 | **1904 passed, 4 skipped**（437 秒） |
| Ruff | 新增/修改文件通过 |
| mypy | `NOT_RUN`（未安装） |

---

## 7. 未解决 / 下一步

1. 向 iFinD 索取**复权因子与前/后复权字段的官方定义**（方向、基准日、累计规则）；否则 G9 不能 PASS；
2. 索取**分红送转明细（每股派现、送转比例、公告日）**；否则公司行为账本无法建立；
3. 申请**历史板块成分接口**；否则 G3 永久 FAIL；
4. 建立 iFinD 板块编码 ↔ b1 板块编码映射后，再提交 383 个板块的预算计划；
5. G1 staging 数据合并入研究库需要**单独授权**（DDL/DML、备份、回滚方案）；
6. Model A/B/A+B 继续冻结；不申请 M7；不训练、不调参、不跑策略收益。

---

## 8. 最终状态

**`M64_PARTIALLY_ACCEPTED`**：接口连通与 G1 最小样本验证成功；G9/G3 受供应商字段与接口能力限制未通过。该状态不代表模型通过，也不构成 M7 授权。
