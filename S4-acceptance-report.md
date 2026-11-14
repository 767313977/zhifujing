# 致富经 zhifujing｜S4 大批次验收报告

> 生成日期：2026-11-14（Asia/Shanghai）
> 分支：`feature/stock-screening-v2`
> 基线：`7eaf798d7e3d71d0ed244d5006e20cb719781266`（S3 报告提交）
> 代码终稿（本报告前）：`f758123f98e29a6e5dfd7d841709025ab1d14eca`
> 结论：**S4 四包已实现、离线测试与受控浏览器全通过**，仍等待下一次集中独立审查。

---

## 1. 基线与提交链

| 项 | 值 |
| --- | --- |
| S4 基线 | `7eaf798d7e3d71d0ed244d5006e20cb719781266` |
| S4 代码终稿 | `f758123f98e29a6e5dfd7d841709025ab1d14eca` |
| 分支 | `feature/stock-screening-v2` |
| 工作区 | 提交链完成后 clean（`git status --short` 为空） |
| 差异规模 | `7eaf798..f758123`：60 个文件、+7670 / −147 |

提交链（均在功能分支，未合并 master、未部署）：

| 提交 | 内容 |
| --- | --- |
| `bf80345` | `docs(s4): record gate 0 baseline and dependency inventory` |
| `88dee91` | `fix(research): segregate cutoff-known unknown and forward evidence` |
| `08f40ea` | `feat(quality): add readonly post-market data coverage dashboard` |
| `c65bf4b` | `feat(sectors): add observed sector resonance research` |
| `f758123` | `test(s4): enforce offline browser gate and integrated acceptance` |
| 本报告 | `docs(s4): finalize evidence and acceptance report`（紧随其后提交） |

Gate 0 依赖矩阵见 [`S4-dependency-inventory.md`](file:///c:/Users/76731/Documents/trae_projects/fupan/S4-dependency-inventory.md)。

---

## 2. S3 交接与 S4 逐文件变更

### 2.1 S3 五包交接

S3 的 A（题材完成证明 / PIT）、B（断板批量加载）、C（个股研究依据）、D（集成 fixture / 浏览器）代码与报告均在此基线内。S4 不重写这些模块，只做以下增量：

- A：把「已知 / 后得 / 后验」从文案约束升级为**结构三桶**；
- B：新增只读质量台，不改断板分类与 P0-B 状态机；
- C：新增板块共振服务，复用 `theme_intersection` 的校验与 `sentiment_next_day` 的聚合口径；
- D：新增离线外呼闸门，补齐 S3 曾触发开盘红现取的流程缺陷。

### 2.2 S4-A 改动

| 文件 | 变更 |
| --- | --- |
| `backend/app/services/stock_research_context.py` | `EVIDENCE_VERSION=research-evidence-v2`；新增 `classify_evidence_time` / `build_time_buckets` / `validate_time_bucket_invariants`；输出 `at_cutoff_known`、`unknown_or_later`、`later_outcome` 三桶；池冲突 `POOL_CONFLICT`；`NOT_IN_POOL` 要求三池可信且日志时点可证明；兼容字段 `as_known_on_day` 只保留已知子集 |
| `backend/app/schemas.py` | 新增 `StockResearchCutoffKnown` / `StockResearchUnknownOrLater`；证据加 `time_bucket` / `source_published_at` / `observed_at`；响应加 `evidence_version` / `cutoff` / `evidence_counts` |
| `backend/app/api/stock.py` | `research-context` 增加 `cutoff`（只支持 CLOSE，其他 400） |
| `frontend/src/api/types.ts` | 同步三桶与时点字段 |
| `frontend/src/components/StockResearchDrawer.tsx` | 三区各自渲染自己的桶；删除同一题材「已知 + 盘后未知」重复；证据表新增「时间桶」列 |
| `tests/test_stock_research_context.py` | 重写为三桶/时点/冲突/守恒测试（20 项） |
| `tests/test_s3_integration.py` | 修正 S3「LATER 属于已知」的旧断言与 fixture 日志时间 |
| `frontend/src/pages/__tests__/StockResearchEvidence.test.tsx` | 三桶渲染、题材不重复、时间桶列 |

### 2.3 S4-B 改动

| 文件 | 变更 |
| --- | --- |
| `backend/app/services/data_quality.py`（新增） | 六来源只读快照：`stock_daily / limit_pool / sector_member / market_sentiment / auction_snapshot / trade_calendar`；`latest_attempt` 与 `latest_valid_snapshot` 分开；单来源按需计算；`days` 上限 250 |
| `backend/app/api/quality.py`（新增） | `GET /api/quality/overview`、`/timeline`、`/egress`；强制 `current_user`，只读 |
| `backend/app/schemas.py` | `QualitySourceItem` / `QualityOverviewOut` / `QualityTimelineOut` |
| `backend/app/main.py` | 注册 quality router |
| `frontend/src/pages/DataQuality.tsx`（新增） | 独立 `/data-quality` 只读控制台：日期/窗口、全部/可信/部分/未采集筛选、展开证据、来源时间线 |
| `frontend/src/App.tsx`、`components/Layout.tsx` | 路由与「复盘 → 数据质量」入口 |
| `frontend/src/pages/{Sentiment,Sectors,Auction}.tsx` | 页头加「查看数据质量」链接 |
| `tests/test_data_quality.py`（新增） | ok+0、先成功后失败、无日志有行、字段空、竞价缺帧、非交易日、未来日、days 上限、只读、无外呼 |

### 2.4 S4-C 改动

| 文件 | 变更 |
| --- | --- |
| `backend/app/services/sector_resonance.py`（新增） | 快照只向过去找；AND/OR 去重；首板/二板/三板+/炸板/冲突/未知分桶；D+1 pooled/中位/上涨比例/再涨停；`member_completion_proof=false` |
| `backend/app/api/sector.py` | `GET /api/sectors/resonance` |
| `backend/app/schemas.py` | `SectorResonanceOut` |
| `frontend/src/components/SectorResonance.tsx`（新增） | 板块选择、AND/OR、市场过滤、分页、统计卡、分组表、逐券表、QuickResearch |
| `frontend/src/pages/Sectors.tsx` | 题材交叉之后挂载共振面板 |
| `tests/test_sector_resonance.py`（新增） | 15 项：AND/OR 去重、冲突不双计、PENDING、缺覆盖、池不可信、快照不前滚、分页稳定、PIT、只读、无外呼 |
| `scripts/export_sector_resonance_sample.py`（新增） | 只读导出真实样例 |
| `reports/s4_resonance/real_2026-09-24_or.json` | 真实历史样例 |

### 2.5 S4-D 改动

| 文件 | 变更 |
| --- | --- |
| `backend/app/external_guard.py`（新增） | `EXTERNAL_DATA_FETCH_ENABLED` 闸门、`ExternalFetchBlocked`、requests 层 egress deny、attempts/blocked/network 计数 |
| `backend/app/config.py` | `external_data_fetch_enabled: bool = True`（与 `SCHEDULER_ENABLED` 分离） |
| `backend/app/main.py` | 启动时仅在开关关闭时安装 deny；`ExternalFetchBlocked → 503` 明确响应 |
| `backend/app/sources/*` | 腾讯 / 开盘红 / iFinD / akshare / 东财新闻与事件 / 同花顺 / 通达信 / 蛋卷 / ETF / 开盘红本地，全部在发网前过闸 |
| `backend/app/services/base.retry_call` | 离线拒绝不重试（不白等退避） |
| `backend/app/services/stock_events.py`、`api/sector.py`、`api/stock.py` | 有本地缓存/回落的路由返回「离线验收模式」说明，不用伪网络异常 |
| `tests/test_external_guard.py`（新增） | 8 项：默认开启、关闭阻断、不重试、requests 层拦截、开盘红入口、板块成员回落 0 发网、DDE 回落、开关与调度器语义独立 |
| `tests/test_s4_query_budget.py`（新增） | 5 项查询预算（数据规模大时不按证券 N+1） |
| `scripts/benchmark_s4.py`（新增） | 只读性能基准 |
| `scripts/export_s4_readonly_evidence.py`（新增） | 只读导出 3 个真实 PIT 案例 + 2 个质量快照 |
| `reports/s4_browser/`、`reports/s4_performance/`、`reports/s4_pit/` | 证据 |

---

## 3. A 包：时点证据 v2

### 3.1 三桶字段

| 桶 | 允许的 `known_by` | 包含 | 不变量 |
| --- | --- | --- | --- |
| `AT_CUTOFF_KNOWN` | `CLOSE` / `PRIOR_DAY` | 观察日日线、可信且日志在当日的池记录、前一日已取到的题材、情绪 | 不得出现观察日之后的 `source_trade_date`；不得含 LATER/同日未知/盘后未知 |
| `UNKNOWN_OR_LATER` | `SAME_DAY_UNKNOWN` / `LATER` / `POST_CLOSE_UNKNOWN` / `UNKNOWN` | 同日/次日取到的题材、无发布时间的 `stock_concept`、无日志/次日日志的池 | `is_forward_known=false` |
| `FORWARD_OUTCOME` | `FORWARD` | D+1 日线与再涨停 | `source_trade_date > observation_date`；`is_forward_known=true` |

每条证据统一带：`claim`、`source_table`、`source_trade_date`、`fetched_at`、`source_published_at`（无则 null）、`observed_at`、`known_by`、`quality`、`reason_code`、`is_forward_known`、`time_bucket`。`validate_time_bucket_invariants()` 校验三桶互斥、并集守恒、桶与 `known_by`/`is_forward_known` 一致。

### 3.2 PIT 判定规则

- `SectorMember.fetched_at` 只证明本站取到时间：`< D` → `PRIOR_DAY`；`= D` → `SAME_DAY_UNKNOWN`；`> D` → `LATER`；缺失 → `UNKNOWN`。只有 `PRIOR_DAY` 进已知。
- `CollectLog.created_at` 只证明任务记录时间：日志在 D 当天 → 池信息按收盘后可获知；日志在 D 之后 → `LATER`；无日志 → `UNKNOWN`。
- `StockConcept` 没有可审计发布时间 → 永远 `POST_CLOSE_UNKNOWN`，已知集合里恒为空。
- 同一股票同时在 up/broken 池 → `POOL_CONFLICT`，不按 `if/elif` 优先级择一。
- `NOT_IN_POOL` 只有涨停/炸板/跌停三池都可信且日志时点可证明时才成立。
- `PREMARKET_0925` 明确 400 拒绝，不假装与 CLOSE 同口径。
- `historical_signal_eligible` 恒为 false。

### 3.3 RED → GREEN

修复前（S3）：`as_known_on_day["themes"]` 含 `LATER`/`SAME_DAY_UNKNOWN`，`concepts` 含 `POST_CLOSE_UNKNOWN`；前端把同一题材先放「已知」再放「盘后未知」。修复后：
- 后端旧断言（`test_later_theme_is_not_claimed_as_known`）改为「LATER 不在已知、只在 unknown」；
- 前端单测新增「同一题材只出现 2 次（unknown 题材 + 涨停天梯），不再重复进 known」；
- 三桶不变量在每次构建时执行，违法直接 `RuntimeError`。

### 3.4 合成反例（≥5）

`tests/test_stock_research_context.py` 覆盖 10 类反例：同日题材、次日题材、无时间题材（分类函数）、concept、池冲突、`NOT_IN_POOL` 需三池可信、池日志次日写入、后验只在后验桶、非法桶混合被拒、`PREMARKET_0925` 被拒。

### 3.5 3 个真实历史只读案例

来源：`backend/data/fupan.db`（`mode=ro&immutable=1`），观察日 `2026-09-24`，证据见 [`real_cases.json`](file:///c:/Users/76731/Documents/trae_projects/fupan/reports/s4_pit/real_cases.json)。

| 代码 | 已知桶 | 后得桶 | 后验桶 | 已知题材 | 后得题材 | D+1 |
| --- | --- | --- | --- | --- | --- | --- |
| 000498 | 3 | 2 | 1 | 无 | 基础建设（LATER） | +1.95%（PARTIAL：D+1 池未验证） |
| 000592 | 3 | 7 | 1 | 无 | 医美/医药/土地流转/旅游/海峡两岸/福建（LATER） | +3.53%（PARTIAL） |
| 000678 | 3 | 2 | 1 | 无 | 机器人概念（LATER） | +10.02%（PARTIAL） |

三例的 `at_cutoff_known.themes` 均为空、`historical_signal_eligible=false`，后验结果没有回流到已知区。

---

## 4. B 包：只读数据质量台

真实库 `fupan.db` 在 `2026-09-24` 的快照（`/api/quality/overview`）：

| 来源 | 状态 | 已观察行 | 板块覆盖 | 完成证明 | 原因 |
| --- | --- | --- | --- | --- | --- |
| stock_daily | VERIFIED_SOURCE | 5,282 | — | — | — |
| limit_pool | VERIFIED_SOURCE | 75 | — | 是 | — |
| sector_member | PARTIAL | 3,757 | 19 / 374（两 taxonomy 合计） | 否 | `NO_MEMBERS_COMPLETION_LOG` |
| market_sentiment | VERIFIED_SOURCE | 1 | — | — | — |
| auction_snapshot | NOT_COLLECTED | 0 | — | 否 | `AUCTION_NOT_COLLECTED`、`AUCTION_0931_MISSING` |
| trade_calendar | VERIFIED_SOURCE | 1 | — | — | — |

- `sector_member` 按 taxonomy 展开后：`kph_selected` 18/270、`kph_industry` 1/104；合计 19/374。**不把「已观察板块数」升级为成员完整证明**。
- 生产副本 `fupan.db` 与研究库 `b1_research.db` 分别读取、分别结论，**没有跨库拼凑**。
- 最新一次失败不会擦掉旧快照：`latest_attempt` 与 `latest_valid_snapshot` 分开，09-23 的「先成功后失败」fixture 显示 `PARTIAL + LATEST_ATTEMPT_FAILED_SNAPSHOT_RETAINED`。
- 该接口不建表、不修库、不补采、不请求外部源；缺表只影响单个来源并标 `DB_TABLE_MISSING`。

---

## 5. C 包：板块主线共振

真实历史只读样例（`2026-09-24`，OR：养老概念 801287 / 旅游 801330 / 福建 801319）见 [`real_2026-09-24_or.json`](file:///c:/Users/76731/Documents/trae_projects/fupan/reports/s4_resonance/real_2026-09-24_or.json)：

| 指标 | 值 |
| --- | --- |
| 成员行 / 唯一成员 | 385 / 366 |
| 已观察板块 | 3 / 3 |
| 成员完成证明 | false |
| 已观测涨停 | 13（首板 10、二板 1、三板+ 2） |
| 炸板 | 2 |
| 冲突 | 0 |
| D+1 样本 / 有效 / 缺失 | 366 / 356 / 10 |
| D+1 平均 / 中位 | −1.94% / −1.43% |
| D+1 上涨比例 | 20.51% |
| D+1 再涨停 | null（D+1 涨停池未验证，不猜） |
| 快照 PIT | `LATER`（09-24 快照是 09-28 之后取到的），`historical_signal_eligible=false` |
| 质量 | `PARTIAL`（无完成证明 + 仅观测） |

反例：A01+A04（A04 无成员快照）→ `UNVERIFIABLE` 且空结果；同一股票同时涨停/炸板 → `POOL_CONFLICT` 且不双计；D+1 未到齐 → `PENDING` 且统计为 null（浏览器与单测均覆盖）。

---

## 6. D 包：离线闸门与浏览器验收

### 6.1 离线 deny 执行证据

隔离后端启动日志（[`backend_start.txt`](file:///c:/Users/76731/Documents/trae_projects/fupan/reports/s4_browser/backend_start.txt)）：

```
Schema 检查通过（FUPAN_SCHEMA_MODE=verify，未执行任何 DDL）
已安装离线网络闸门（EXTERNAL_DATA_FETCH_ENABLED=false）：requests 调用将在发网前失败
外部数据现取已被禁用（EXTERNAL_DATA_FETCH_ENABLED=false）
定时采集已关闭（scheduler_enabled=false）
```

浏览器故意触发一次**真会走到来源入口**的按需同步 `POST /api/stock/600000/sync?days=5`：

- HTTP 503，响应 `{"offline": true, "reason": "EXTERNAL_DATA_FETCH_DISABLED"}`；
- 后端日志：`外部取数被离线闸门阻止：iFinD stock`；
- egress 计数（[`egress_after.json`](file:///c:/Users/76731/Documents/trae_projects/fupan/reports/s4_browser/egress_after.json)）：`attempts=1, blocked=1, network_attempts=0, network_blocked=0`。

即：**1 次试图外连、1 次被阻断、0 次真正发网**。

另外，故意访问缺缓存的旧按需 API `/api/sectors/members?code=A04&date=2026-09-21`：返回 200 + `note="离线验收模式：禁止外部现取，本地没有该板块的成员快照"`，egress 仍为 0 发网。单测 `test_board_members_falls_back_without_network` 与 `test_kaipanhong_source_is_blocked_before_network` 覆盖同一条路径。

### 6.2 浏览器用例

Chrome headless（CDP），临时 SQLite + 临时账号 + `SCHEDULER_ENABLED=false` + `EXTERNAL_DATA_FETCH_ENABLED=false` + `FUPAN_SCHEMA_MODE=verify`。**25 项全部通过、0 console error**，详情见 [`browser_run.md`](file:///c:/Users/76731/Documents/trae_projects/fupan/reports/s4_browser/browser_run.md) 与 [`browser_results.json`](file:///c:/Users/76731/Documents/trae_projects/fupan/reports/s4_browser/browser_results.json)。

覆盖：
- 题材 AND/OR、部分覆盖、缺覆盖 `UNVERIFIABLE`；
- 研究依据三桶互斥：LATER 题材不在已知、D+1 不在已知；
- `?stock=&as_of=` 刷新保留、后退/前进、Esc 关闭、账户切换清空；
- 数据质量台：数据/部分/未采集/详情/筛选/未来 pending；
- 板块共振：分桶、AND/OR 去重、缺覆盖、0 样本、D+1；
- 旧 `/limit-up`、`/auction`、`/sentiment`、`StockLink`、晋级漏斗不回归；
- 1280×800、1920×1080、3840×2160、390×844 四视口截图；
- 0 console error、0 未授权真实外呼、0 生产库写入、0 定时采集。

截图 SHA256 全部记录在 `browser_results.json` / `browser_run.md`，共 10 张。

---

## 7. 全量工程门禁

| 命令 | 结果 |
| --- | --- |
| `python -m compileall -q backend scripts` | 通过（退出码 0） |
| `python -m ruff check backend scripts tests` | `All checks passed!` |
| `python -m pytest -q` | **1471 passed, 3 skipped**, 4 warnings（耗时 363.72s） |
| `npm test` | **14 files / 140 tests passed** |
| `npm run lint` | `Found 0 warnings and 0 errors`（130 files） |
| `npm run build` | 成功（`✓ built in 447ms`） |

未删除测试、未增加 skip、未降低断言。

---

## 8. 性能表

来源：[`benchmark.json`](file:///c:/Users/76731/Documents/trae_projects/fupan/reports/s4_performance/benchmark.json)（`fupan.db` 只读，含 SQL 条数 / 耗时 / Python 峰值内存）。

| 用例 | 规模 | SQL | 耗时 | 峰值内存 |
| --- | --- | --- | --- | --- |
| quality overview | 30/60/120/250 日 | 4 | 66.0 / 2.8 / 2.5 / 3.1 ms | ≤ 1.7 MB |
| quality timeline(limit_pool) | 30/60/120/250 日 | 4 | 10.1 / 12.9 / 39.5 / 63.2 ms | ≤ 3.3 MB |
| sentiment next-day(first_board) | 20/60/120/250 日 | 5/5/61/191 | 59 / 57 / 306 / 1016 ms | ≤ 10.7 MB |
| sector resonance | 1/2/3/10 板块，AND/OR | 12–15 | 111–191 ms | ≤ 4.8 MB |
| research-context | 单股单日 | 12 | 77.5 ms | 2.2 MB |

- 质量台优化后：timeline 250 日从 3.9s 降到 63ms（只计算被选来源）；overview 只扫目标日，不再扫整个窗口。
- `sentiment_next_day` 的 SQL 随**天数**增长（约 1–2 条/日），与股票只数无关；S4 未改其聚合口径与状态机。
- 不声明线上 SLA；以上是本机只读实测。

---

## 9. 边界、冻结与只读复核

### 9.1 未改动

- `git diff --name-status 7eaf798..HEAD` 未包含生产 DB、`forward_c0/*`、C0 五个脚本、`deploy/*`、调度配置。
- C0 五个 Git blob 与 S3 一致：

| 文件 | blob |
| --- | --- |
| `scripts/forward_collect_tushare.py` | `1408f9e3…fdf2a` |
| `scripts/forward_store.py` | `8064d670…fab5f` |
| `scripts/forward_track_c0.py` | `6ff3eb08…9abe` |
| `scripts/forward_universe.py` | `2f8979d2…2ca0` |
| `backend/app/services/factor_score.py` | `32b6f2ff…e80f` |

### 9.2 冻结信号

| 文件 | SHA256 |
| --- | --- |
| `signals_2026-11-11.jsonl` | `1F80D1B6785B930FC9A922F027A323A36F5DDCF122D2EC865810F33523F07B37` |
| `signals_2026-11-11.meta.json` | `44301EB5E00867E4E19D51C7DEA524AF0897CED0E7A5002CAC2F530E3FA2DD50` |

与 S3 完全一致，未重生成、未改写。

### 9.3 三个库

| 库 | 大小 | mtime | integrity_check |
| --- | --- | --- | --- |
| `fupan.db` | 513,519,616 B | 2026-10-31 22:26:44 | ok |
| `b1_research.db` | 1,441,378,304 B | 2026-11-09 12:23:12 | ok |
| `forward_research.db` | 67,055,616 B | 2026-11-11 17:17:58 | ok |

S4 全程以 `mode=ro&immutable=1` 读取；大小/mtime 与 Gate 0 完全一致。

---

## 10. 回滚单元方案（未执行）

S4 的改动可整体或按包回退，互不依赖数据库迁移：

```powershell
# 方案 A：按提交逆序 revert（保留历史，推荐）
git revert --no-commit f758123 c65bf4b 08f40ea 88dee91 bf80345
git commit -m "revert(s4): roll back s4 batch"

# 方案 B：仅回退某个包
git revert --no-commit 88dee91   # 只回 A
git revert --no-commit 08f40ea   # 只回 B
git revert --no-commit c65bf4b   # 只回 C
git revert --no-commit f758123   # 只回 D
```

不涉及生产库、调度、C0 或部署，因此回滚只需代码回退；`reports/` 下的证据文件可保留。

---

## 11. 尚未验收的边界

- **真实交易日 / 真实题材全量**：本批次全部为合成库 + 只读历史库，未做真实交易日题材全量验收 → `THEME_REAL_DATA_ACCEPTANCE_PENDING`。
- **真实竞价数据**：生产副本 `auction_snapshot` 仍为 0 行，竞价质量台显示 `NOT_COLLECTED`；没有调用腾讯或任何外部源 → `REAL_AUCTION_DATA_NOT_YET_VERIFIED`。
- **线上账号 / 部署**：未部署 zhifujing.top、未合并 master → `PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED`、`P13_RELEASE_NO_GO`。
- **历史数据缺口**：真实库的板块成员快照只覆盖部分日期/taxonomy，仍需单独审查 → `HISTORICAL_DATA_GAP_UNDER_REVIEW`。
- **D+1 再涨停比例**：真实库多数日期 D+1 涨停池日志不可信时为 null，未用涨跌幅猜。

---

## 12. 状态词

- `S4_IMPLEMENTED_PENDING_INDEPENDENT_REVIEW`
- `S4_PIT_EVIDENCE_SEGREGATED`
- `S4_LOCAL_DATA_QUALITY_READY`
- `S4_SECTOR_RESONANCE_READY`
- `S4_OFFLINE_EGRESS_GATE_PASSED`

保留：
- `THEME_REAL_DATA_ACCEPTANCE_PENDING`
- `REAL_AUCTION_DATA_NOT_YET_VERIFIED`
- `PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED`
- `P13_RELEASE_NO_GO`
- `HISTORICAL_DATA_GAP_UNDER_REVIEW`

---

## 附录：外部网络调用统计

| 项 | 次数 |
| --- | --- |
| 试图外部取数（`ensure_external_fetch`） | 1 |
| 被离线闸门阻断 | 1 |
| 真正发网（`network_attempts`） | **0** |
| 被 requests 层拦截 | 0 |

> 0 次真正发网是本批次浏览器与测试的硬门禁；上面的 1 次「试图」来自故意触发的 `POST /api/stock/600000/sync`。两者不能混为一谈。
