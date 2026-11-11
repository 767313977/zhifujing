# 致富经 B3：前瞻数据接入决策与受控验证准备

日期：2026-11-11
状态：`B3_HISTORICAL_RESEARCH_CLOSED` / `FORWARD_PAPER_TRACKING_CANDIDATE` /
`FORWARD_TRACKING_NOT_STARTED` / `PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED` / `P13_RELEASE_NO_GO`

本轮不采集、不建正式库、不建定时任务、不写业务库、不调用供应商接口。

---

## 0. 本轮结论

| 项 | 结论 |
| --- | --- |
| B1 候选范围 | 历史池 5,108 只 → 前瞻可用 **5,076** 只；**排除 32 只北交所**（新旧代码映射未确认）；清单哈希 `3e200c38…`，截止 **2026-10-19** |
| 新库模型 | `forward_research.db` 四表设计完成，与 `StockDaily` ORM 及 `load_matrix` **离线验证兼容** |
| iFinD 批量能力 | **仍无证据**。只能证明单票历史行情可用；「每日 6~10 次取全市场」未被证实 |
| 下一步 | 先做 **5 次请求**的受限只读试验；试验不通过则转 Tushare，不开发跑不动的采集器 |

---

## 1. B1 证券身份与候选范围（修订版）

### 1.1 定义

> **前瞻池 = 历史研究库内出现过的全部证券 ∩ 身份可确认的证券。**
> 不按 `startswith("920")` 放行；不自动纳入历史集合以外的新证券。

落地方式：由 `scripts/forward_universe.py` 只读扫描 `history.db`，生成机读清单
`backend/data/c0_universe_b1.json`（本地产物，不入库），并将其版本与哈希写进每份
前瞻快照的 meta。缺清单时 **forward 直接拒绝**，不允许临时拼一个池。

### 1.2 实测口径（数据截止 2026-10-19）

| 市场 | 只数 | 可前瞻 | 说明 |
| --- | ---: | ---: | --- |
| 沪主板 | 1,634 | 1,634 | 代码段未迁移 |
| 科创板 | 583 | 583 | 同上 |
| 深主板 | 1,469 | 1,469 | 同上 |
| 创业板 | 1,390 | 1,390 | 同上 |
| 北交所 | **32** | **0** | 920 段，新旧映射未确认 |
| 合计 | **5,108** | **5,076** | 排除 32 |

清单哈希（对 `code|name|board` 排序后取 SHA256）：

```
3e200c3808f22fd00f88cb8bf44bda1839fcee9b79e5cfac51c11e47a717062b
```

可复现命令：

```powershell
.venv\Scripts\python.exe scripts\forward_universe.py --out backend\data\c0_universe_b1.json
.venv\Scripts\python.exe scripts\forward_universe.py --out backend\data\c0_universe_b1.json `
  --verify 3e200c3808f22fd00f88cb8bf44bda1839fcee9b79e5cfac51c11e47a717062b
```

两次生成结果一致（`OK`），说明清单对同一份 `history.db` 是确定性的。

### 1.3 被排除的 32 只北交所（待身份确认）

920025 920028 920047 920059 920071 920079 920083 920093 920107 920117
920126 920136 920138 920156 920161 920181 920193 920200 920201 920202
920220 920223 920229 920238 920258 920268 920269 920288 920289 920298
920493 920873

排除理由（写死在清单的 `identity_note` 里）：北交所 2025-10-09 完成存量代码向 920 段
切换，离线只能看到 `history.db` 里的 920 代码与名称，**看不到官方「旧代码 ↔ 新代码」
映射**。按任务书要求，身份或映射无法确认时禁止对相关证券生成正式前瞻信号。

> 代价必须写明：前瞻池比历史少这 32 只，严格说是**池子缩小**。因为 C0 取 Top20，
> 这 32 只占全池 0.63%，对 Top20 的影响通常为零，但并非严格等价。等官方映射确认后
> 把这 32 只标记为 `forward_eligible=true` 并**换清单版本**，不能就地改。

### 1.4 forward 与 replay 的池子不同（有意为之）

| 模式 | 池子 | 原因 |
| --- | --- | --- |
| `replay` | 全 `matrix.codes`（不受清单影响） | 复现历史口径，必须与历史 C0 逐项一致 |
| `forward` | 清单内 `forward_eligible` | B1 前瞻池，排除未确认证券 |

这一点有专门测试：`test_replay_ignores_universe`、`test_forward_universe_excludes_unverified`。
后果是 **forward 与 replay 在同一历史日可能给出不同的 Top20**（概率极低但存在），
这是 B1 的已知偏离，不能当成 bug。

### 1.5 版本

- `STRATEGY_VERSION`：`c0-2026-11-10` → **`c0-2026-11-11-b1`**
- 候选池版本：`b1-2026-11-11`（清单 `version` 字段 + `universe_sha256`）
- 历史 C0 的公式、窗口、权重、排序全部未动；只有**前瞻池**按 B1 收窄

风险标记保持：`SURVIVORSHIP_BIAS`（历史库缺退市股）、`ST_STATUS_PARTIAL`
（名称可能是当前名）、`UNIVERSE_TRUNCATED_BSE`（本轮新增，指 32 只被排除）。

---

## 2. iFinD 接口能力与权限核对

判断标准：**有代码或已记录实测**才算「已证实」；只有推理算「未证实」。

| # | 问题 | 结论 | 依据 |
| --- | --- | --- | --- |
| 1 | 按交易日批量取全市场 OHLC | **未证实** | 代码里只有单票 `stock_history` 与选股 `search_stocks_full`，无按日全市场行情接口 |
| 2 | 是否含官方 `pct_chg` | 已证实（单票） | `stock_history` 查询语句显式要「涨跌幅」 |
| 3 | 是否含成交额 | 已证实（单票） | 同上；单位元，与腾讯实测对账差 19 元 |
| 4 | 是否完整覆盖沪深及所需北交所证券 | **未证实** | 生产不做全市场落库；北交所覆盖未知 |
| 5 | 每次请求最大证券数量 | 已证实（高频接口） | `ifind_max_symbols=10`；批量行情上限未知 |
| 6 | 是否分页/抽样/截断 | 已证实（单票接口） | >90 日历日会抽样丢弃；CSV ≤1000 行；表格 100 行 |
| 7 | CSV 下载是否适用于完整行情 | **未证实** | 现有 CSV 只用于**选股结果集**，不是行情 |
| 8 | 额度按什么计 | 部分证实 | 配置 `ifind_monthly_quota=5452`；CSV 下载不计次；批量接口计费方式未知 |
| 9 | 是否允许本地留存与研究用途 | **未证实** | 仓库无授权信息 |
| 10 | 是否与生产共享额度 | 推定共享 | 同一 token/账号，代码里只有一个配额配置 |

结论：**没有证据证明「每日 6~10 次取全市场」，因此不写成已验证事实。**
在试验通过前，采集器不开发。

---

## 3. `forward_research.db` 与 ORM 兼容性设计

### 3.1 表结构（`scripts/forward_store.py::SCHEMA_SQL`）

```sql
stock_daily(trade_date, code, name, open, high, low, close, pct_chg,
            volume, amount, turnover, board, source, pct_source,
            raw_code, data_version, ingested_at, PRIMARY KEY(trade_date, code))
trade_calendar(trade_date)
data_revision(domain, revision, updated_at)
ingest_log(id, trade_date, status, rows, message, created_at)
```

与 `app.models.StockDaily` 的关系：

- **列名完全一致**，包含你特别指出的 `turnover`（历史库里也有这一列，
  虽然 C0 用不到，但 `select(StockDaily)` 这类整表查询需要它存在）；
- 只**新增**列：`board` / `source` / `pct_source` / `raw_code` / `data_version` /
  `ingested_at`，不改任何既有列名与语义；
- `pct_source` 默认 `official`，即新库一律记官方涨跌幅。

### 3.2 已完成的离线验证（未创建正式库文件）

| 验证 | 测试 |
| --- | --- |
| DDL 覆盖 ORM 全部列（含 turnover） | `test_schema_covers_every_orm_column` |
| 重复建表幂等 | `test_schema_is_idempotent` |
| `select(StockDaily)` 整表查询可跑 | `test_orm_and_backtest_read_new_schema` |
| `research_backtest.load_matrix` 直接读新库 | 同上（校验 code/close/pct/amount） |
| 只读连接拒绝写 | `test_readonly_connection_rejects_writes` |

也就是说，以后采集脚本只要把数据写进新库，**回测与前瞻工具不需要改一行代码**，
只把 `--db` 指过去。

### 3.3 读写分离

- 独立采集脚本：`forward_store.connect()`（读写）；
- `forward_track_c0`：`--db` 指向新库，内部一律 `mode=ro`；
- 与 `history.db` / `fupan.db` 物理隔离，不共享连接、不共享文件。

---

## 4. 官方 `pct_chg` 对账测试设计

首次拿到合法数据后，取**重叠的至少 20 个交易日**（两库都有数据的日期），逐项比较：

| # | 检查 | 计算 | 判定 |
| --- | --- | --- | --- |
| 1 | 官方 vs 历史 `pct_chg` | 逐样本差 | 记录均值/95 分位/最大绝对值，已知范围 0.0000~0.43pp |
| 2 | `rev20` | 同一窗口、同一 `pct_chg` 口径分别重算 | 差的分布与最大绝对值 |
| 3 | `illiq20` | 同上（用各自 `amount`） | 相对误差 |
| 4 | 当日横截面排名 | 逐日 Spearman 相关 + 平均名次位移 | 记录最低相关日 |
| 5 | Top20 名单变化率 | `|A△B| / 20` | 逐日记录 |
| 6 | 第 20 名附近替换 | 名次 18~22 的进出情况 | 记录替换次数与股票 |

判定规则（事先写死，避免事后解释）：

- 平均名次位移 ≤ 1 位、Top20 变化率 ≤ 10%：视为**同口径**，可直接沿用历史结论；
- 超出：标记为**新的前瞻研究版本**（`pct_source=official` + 新版本号），
  **不得直接继承旧版回测表现**，需要重新积累前瞻样本。

只输出差异报告，**不修改** `history.db`，也**不重置**历史 C0 结论。

---

## 5. 小规模接口试验预算（待你批准后才执行）

| 项 | 限制 |
| --- | --- |
| 外部请求总数 | **≤ 5 次** |
| 请求内容 | 只查 1 个交易日、只取行情字段；不拉全市场历史 |
| 允许写 | 仅 stdout / 临时内存；**不写任何 DB 文件** |
| 禁止 | 生产库、正式前瞻库、Token 打印、全量下载 |
| 必须记录 | 真实请求数、返回行数、字段名、单位、耗时、错误码、配额消耗 |
| 失败即停 | 鉴权/权限/额度/截断/缺字段 → 记录后停止，不重试 |

试验要回答的唯一问题：**能否用 ≤10 次请求拿到某日全市场 OHLC + 官方 pct_chg + 成交额。**

- 能 → 申请 40 日回补授权；
- 不能 → 直接建议改 Tushare（`daily(trade_date=...)` 天然按日全市场），
  不再投入开发 iFinD 采集器。

---

## 6. 正式 40 日回补的真实调用成本

按「每日一次全市场」的三种可能形态估算（单位：请求数）：

| 形态 | 单日 | 40 日回补 | 对月预算 300 的影响 |
| --- | ---: | ---: | --- |
| A：一次返回全市场 | 1 | 40 | 占 13% |
| B：按代码段 6 段 | 6 | 240 | 占 80% |
| C：按代码段 10 段 | 10 | 400 | **超预算**，需拆两个月或另行批准 |

按任务书预算（每日 ≤20、每月 ≤300、速率 ≤1 req/s、临时错误重试 ≤2 次）：

- 形态 A/B 可在**单月内**完成回补；
- 形态 C 必须拆成两个月，或把回补降为「只补 20 日 + 逐日累积」；
- 若供应商按「数据单元」而非请求计费，需先拿计费说明再重算，**不能套用上面的请求数**。

单票接口（约 5,100 次/日）在任何预算下都不可行，已排除。

---

## 7. 需要你签字确认的外部数据访问范围

请逐条确认；未确认项一律保持关闭：

1. [ ] 允许使用 iFinD 账号做**每日全市场行情批量提取**（新增用途）；
2. [ ] 允许把提取结果**本地留存**于 `backend/data/forward_research.db` 并用于研究；
3. [ ] 允许 **5 次请求**的受限只读试验（§5）；
4. [ ] 允许试验通过后做**40 个交易日**回补（请求上限见 §6，建议 ≤240 次）；
5. [ ] 确认研究用量与生产共用配额时可接受的占用比例（建议 ≤10%）；
6. [ ] 北交所 32 只的处理：确认本轮**暂不纳入**前瞻，待官方映射；
7. [ ] 确认新库使用**官方 `pct_chg`**（`pct_source=official`）；
8. [ ] 若 iFinD 无批量能力，是否同意转 Tushare Pro（需另行确认其积分/权限）。

---

## 8. 本轮代码与测试

| 文件 | 改动 |
| --- | --- |
| `scripts/forward_store.py` | 新增：四表 DDL、字段映射、校验、读写/只读连接 |
| `scripts/forward_universe.py` | 新增：B1 清单生成、哈希、`--verify` |
| `scripts/backtest_reversal.py` | `_day_candidates` 增加可选 `codes`（前瞻池），历史路径不变 |
| `scripts/forward_track_c0.py` | 版本升到 `c0-2026-11-11-b1`；forward 读取 B1 清单并记录到 meta；缺清单拒绝 |
| `tests/test_forward_store.py` | 新增 18 项离线测试（含 6 种代码段） |
| `tests/test_forward_universe.py` | 新增 6 项清单测试 |
| `tests/test_forward_track_c0.py` | 新增 4 项（清单过滤、重放不受影响、缺清单拒绝、风险标记） |

```
$ .venv\Scripts\python.exe -m pytest -q
1138 passed, 3 skipped, 4 warnings in 232.83s (0:03:52)
```

真实库实测：`snapshot --mode forward` → `exit=3`（缺交易日历），不生成任何文件；
`snapshot --mode replay --date 2026-09-15` → 正常，池 5,069，标记 `REPLAY_ONLY`。

未创建 `forward_research.db`，未连接任何供应商，未修改 `history.db` / `fupan.db`，
未读取 35 个敏感数据库，未创建调度任务，未部署，未下任何真实订单。
