# 致富经 S3 验收报告

- 文档版本：S3-acceptance-v1.0
- 日期：2026-11-14（`Asia/Shanghai`）
- 分支：`feature/stock-screening-v2`（未合并 master、未部署）
- S2 基线：`251cac4c1eadd3e801c798bb3b1180202b05562d`
- S3 起始 HEAD：`40b13273bb92ced28e177ee32f4645af65fec7b7`
- S3 代码终稿（完整测试）：`8b110f8e45d25ed4953c9b49e257784794e393c2`
- 报告提交：本文件提交（父提交为 `8b110f8`）
- 状态：`S3_IMPLEMENTED_PENDING_INDEPENDENT_REVIEW`

---

## A. Git 基线与提交

| 包 | 提交 | 说明 |
| --- | --- | --- |
| A | `dc3c614` | `fix(themes): require real completion evidence and point-in-time flags` |
| B | `177471d` | `perf(sentiment): batch historical failed-promotion analysis` |
| C | `cdaf230` | `feat(research): expose as-of evidence and later outcomes` |
| C-fix | `7dc7435` | `fix(research): send as_of to research-context`（浏览器验收发现） |
| D | `8b110f8` | `test(s3): integrated fixtures browser acceptance and safety gates` |
| 报告 | 本文件提交 | `docs(s3): add acceptance report` |

- 起始工作区 clean；`git diff --check` 无输出。
- `origin/master` 仍为 `44f0912d9f754e253b06e1296cb1f8b381f158a0`，未合并、未强推。
- S3 diff（`40b1327..8b110f8`）只包含：业务源码、测试、`S3-dependency-inventory.md` 和
  `reports/s3_*` 证据；**没有** C0 / `forward_c0/*` / 数据库文件 / 采集任务 / 调度改动。

---

## B. A 包：题材交叉 v2（质量证据 + PIT）

### B1. 原逻辑与新逻辑

| 项目 | S2 原逻辑 | S3 新逻辑 |
| --- | --- | --- |
| 观察覆盖 | `global_sectors` = 跨 taxonomy 的 distinct sector_code | `observed_sector_count` = **同 taxonomy**、按 `(taxonomy, sector_code)` 去重 |
| 期望板块 | 按所选 taxonomy 的 `SectorBasic` 数 | 同样按所选 taxonomy；与 observed 同一过滤 |
| 完成证据 | `global_sectors >= expected` 就置 `has_completeness_evidence=true`，可升级 `VERIFIED` | `has_completion_proof` 只有在存在成员级完成协议时才为 true；**当前数据源恒为 false** |
| 兼容字段 | — | `has_completeness_evidence` 严格映射到完成证明（恒 false）；`sector_count/expected_sectors` 保留为观察覆盖别名 |
| 状态上限 | 可 `VERIFIED` | 有快照时最高 `PARTIAL`；AND 缺板块 `UNVERIFIABLE`；无快照 `NOT_COLLECTED` |
| 版本 | — | `quality_version = "theme-quality-v2"` |

### B2. RED 反例与修复后值

| 反例 | 旧实现 | 新实现 |
| --- | --- | --- |
| taxonomy A 10 个基础板块只观察 1 个；taxonomy B 观察 20 个 | `global=21 >= expected_A=10` → 误标完整，可 `VERIFIED` | `observed=1 / expected=10`，`has_completion_proof=false`，`PARTIAL` |
| A 的 10/10 板块各仅 1 行成员 | 板块数量达标 → 误标完整 | `coverage_status=COMPLETE_OBSERVED_COUNT`，但 `has_completion_proof=false`，`PARTIAL`，附 `OBSERVED_INTERSECTION_ONLY` |
| 所选板块 A/B 都有行但 A 只有部分成员 | 结果被当成全量交集 | 仍返回结果，但标「已观测交集」；`PARTIAL` |
| AND 有一个板块完全无行 | 可能返回空交集 | `UNVERIFIABLE` + `AND_UNVERIFIABLE`，空结果不冒充「0 只」 |
| OR 缺一个板块 | 可能当作完整并集 | `PARTIAL`，保留可用部分 |

测试：
`tests/test_theme_intersection.py` 31 条（新增 7 条覆盖证据/PIT/分页/无外部取数）。
前端 `ThemeIntersection.test.tsx` 9 条（新增 PIT 与覆盖口径展示）。

### B3. PIT（历史可知时点）

- `known_by_observation_time`：
  - 快照在观察日之前取到 → `true`；
  - 观察日之后才取到 → `false`；
  - 同一天取到 → `null`（无法区分盘前/盘后，不猜）；
  - 看最新视图 → `null`。
- `historical_signal_eligible`：本轮恒为 `false`，只读展示，不进入策略。
- `snapshot_age_days`、`fetched_at`、`source_snapshot_date` 同时返回。
- UI 常驻显示「无法证明盘前已知 / 快照晚于观察日，仅可复盘」与「历史信号资格：否」。

### B4. 真实只读案例（`b1_research.db`，2026-09-24）

| 案例 | 已观测命中 | 覆盖（同 taxonomy） | 状态 | SQL | 耗时 |
| --- | --- | --- | --- | --- | --- |
| 芯片×算力 AND | 362 | 259/270 | PARTIAL | 7 | 165.0ms |
| 芯片×算力×人工智能 AND | 221 | 259/270 | PARTIAL | 7 | 72.5ms |
| 10 板块 OR | 4,811 | 259/270 | PARTIAL | 7 | 118.0ms |

证据：`reports/s3_quality/snapshot_coverage_examples.json`（含 taxonomy A/B 合成反例）。
`has_completion_proof` 全部为 `false`；生产副本只有 19/270 板块，保持
`THEME_REAL_DATA_ACCEPTANCE_PENDING`。

---

## C. B 包：情绪次日 v3（批量断板 + 守恒）

### C1. 实现

- 新增 `limit_funnel.load_funnel_window_inputs(session, days, calendar)`：
  - 一次批量读取窗口内池 / 日线 / 基础资料 / 题材 / 涨停原因 / 采集日志；
  - 逐日构造 `FunnelInputs`，**完全复用** `classify_promotion` / `build_promotion_funnel`；
  - 日线按「每天真正需要的前一日涨停 ∪ 当日涨停 ∪ 当日炸板」加载，按 10 日分块，
    避免「全窗口代码 × 全窗口日期」近百万行扫描。
- `sentiment_next_day` 在 `group=failed_promotion` 时只调用一次窗口加载层；
  其它分组路径不变。
- `GET /api/limit/promotion-funnel`、P0-B 255 日行为、逐券分类规则均未改。
- 新增 `METRIC_MISSING`：D 日情绪指标缺失时保留逐券统计，但不放入任何档位；
  `quality.metric_missing_days` 返回缺失日期，前端常驻提示。
- 前端档位表对 `stat_day_count<5` 或 `valid_count<30` 标「小样本」；
  断板 120/250 日提示「计算可能较慢」。

### C2. 等价性

6 个 S2 报告日期，窗口批量 vs 逐日加载的断板代码逐日完全一致：

| 日期 | 断板数 | equal |
| --- | --- | --- |
| 2026-09-17 | 74 | true |
| 2026-09-18 | 33 | true |
| 2026-09-21 | 55 | true |
| 2026-09-22 | 76 | true |
| 2026-09-23 | 46 | true |
| 2026-09-24 | 36 | true |

`tests/test_sentiment_next_day.py::test_window_loader_matches_per_day_loader` 对合成 fixture 的
`FunnelInputs` 做 dataclass 全等断言；`test_window_loader_query_budget_for_250_days` 锁定
250 日 SQL 上限。

### C3. 性能（同一机器、同一只读副本）

| 窗口 | S2 旧 SQL | S3 新 SQL | S3 耗时 | 有效观测 |
| --- | --- | --- | --- | --- |
| 20 日 | 185 | 33 | 186.5ms | 901 |
| 60 日 | 545 | 82 | 521.1ms | 3,143 |
| 120 日 | 1,085 | 153 | 1,154.9ms | 6,983 |
| 250 日 | 2,255 | **300** | **2,500.9ms** | 12,874 |

- 250 日 SQL 降低 **87%**，达到「≤300 SQL/250 日」目标；
- 峰值内存 **47.84MB**（tracemalloc；开启 tracing 时耗时不作 SLA）；
- 首板 / 全部涨停 250 日仍约 255 SQL，未被批量层影响。

### C4. 口径守恒

- `sample_count = valid_count + missing_count`；
- 再次涨停分子只数有效日线样本，`0 <= again <= valid`，`again + not_again = valid`；
- `avg_pct == security_weighted_avg_pct`（pooled），`day_equal_weight_avg_pct` 独立保留；
- `median_pct` 是全部有效证券-交易日观测的 pooled 中位数；
- D+1 未成熟 `PENDING`、池不可信 `UNVERIFIED`、部分缺样本 `PARTIAL`，null 不转 0；
- D+1 涨停池不可信时 `limit_up_again_ratio=null`；不按日线涨幅猜涨停。

证据：`reports/s3_quality/sentiment_golden_comparison.json`（6 日指标、批量等价、性能、
合成不变量）。

---

## D. C 包：个股观察日研究依据

### D1. 新接口

`GET /api/stock/{code}/research-context?as_of=YYYY-MM-DD`

- 只读：`limit_pool`、`stock_daily`、`market_sentiment`、`sector_member`、`stock_concept`、
  `collect_log`、`trade_calendar`、`stock_basic`；
- 不调用 `syncStock`、不抓外部行情/公告/题材、不写库；
- 代码必须 6 位；非交易日 / 未知代码返回 400，不滚动到别的日期；
- 响应严格三段：`as_known_on_day` / `later_outcome` / `evidence[]`，
  `historical_signal_eligible=false`。

### D2. 真实只读样例

| 案例 | 代码 | 状态 | 观察日已知 | 题材时点 | D+1 | 证据条数 |
| --- | --- | --- | --- | --- | --- | --- |
| 涨停+题材 | 002119 | PARTIAL | UP · 2 板 | LATER | +0.34% | 6 |
| 涨停无题材 | 002413 | PARTIAL | UP · 1 板 | LATER | +2.34% | 6 |
| 炸板 | 000910 | PARTIAL | BROKEN | LATER | −10.00% | 5 |
| 停牌缺日线 | 600136 | PARTIAL | NOT_IN_POOL | LATER | UNVERIFIABLE/null | 5 |
| 无池记录 | 600889 | PARTIAL | NOT_IN_POOL | LATER | UNVERIFIABLE/null | 5 |
| PIT 风险 | 002119 | — | `THEME_PIT_UNKNOWN`、`CONCEPT_POST_CLOSE_UNKNOWN` | 全部 LATER | — | — |
| 观察日无采集 | 002119 @ 2026-07-03 | PARTIAL | NOT_IN_POOL | — | — | — |

证据：`reports/s3_quality/stock_context_examples.json`（逐字段 claim / source_table /
source_trade_date / fetched_at / known_by / quality / reason_code / is_forward_known）。
真实库中的 `sector_member.fetched_at` 多在观察日之后（2026-09-25 至 11-09），因此题材一律
`LATER`，不冒充当时已知。

### D3. 前端

- 侧栏新增 `evidence` Tab（「依据」），与既有 6 个 Tab 并存；
- `?stock=600000&as_of=2026-09-24` 可刷新恢复观察日（`as_of` 独立于页面 `date`）；
- 题材交叉 / 涨停复盘行新增「据」按钮，直接打开证据 Tab；
- 情绪次日逐日行新增「当日池」链接（组级数据没有个股代码，不伪造个股入口）；
- 未登录 / 换账户清空侧栏与缓存；Esc、焦点恢复、前进后退、窄屏均保留。

---

## E. D 包：浏览器与集成验收

### E1. 浏览器执行

- Chrome **157.0.8103.79** headless（CDP），本地服务 `127.0.0.1:8012`；
- 临时合成库 `%TEMP%\s3_e2e\s3.db`（不是生产/研究库）；
- 启动日志：`SCHEDULER_ENABLED=false`、`Schema 检查通过（未执行 DDL）`；
- 两个临时账户 `s3a` / `s3b`；
- **16/16 检查 PASS，控制台 0 error**。

| 检查 | 结果 |
| --- | --- |
| 题材 AND：已观测命中、2/2 所选覆盖、3/10 全市场覆盖、仅已观测交集 | PASS |
| 缺一板块 AND → UNVERIFIABLE，不显示空交集 | PASS |
| 跨 taxonomy 20/20 仍不显示「可验证」（无完成证明） | PASS |
| 情绪：逐券均值 + 日等权 + `next-day-aggregation-v2` | PASS |
| 断板 120 日切换与慢计算提示 | PASS |
| 研究依据 Tab：已知 / 盘后未知 / D+1 后验三段 | PASS |
| 题材 LATER、历史信号资格否、D+1 −5.00% | PASS |
| Esc 关闭、焦点恢复 | PASS |
| `?stock` + `?as_of` 刷新/后退/前进恢复 | PASS |
| 390×844 窄屏 | PASS |
| 切换账户后侧栏不继承 | PASS |
| `/limit-up` 旧 StockLink、`/auction` 正常 | PASS |
| 控制台 error | 0 |

截图（`reports/s3_browser/`，SHA256 见 `browser_results.json`）：
`theme_partial_1280x800.png`、`theme_unverifiable_1280x800.png`、
`theme_cross_taxonomy_1280x800.png`、`sentiment_1280x800.png`、
`sentiment_1920x1080.png`、`sentiment_3840x2160.png`、
`research_evidence_1280x800.png`、`research_evidence_390x844.png`、
`limit_page_1280x800.png`。

### E2. 浏览器环境说明（如实记录）

- 第一次浏览器脚本运行时，`/sectors` 自动选中板块并调用既有 `/sectors/members`，
  因合成库 2026-09-25 成员快照缺失，触发了开盘红现取**尝试**（返回 `errcode=1020`，未成功）。
  该端点不是 S3 新接口，但违反了本轮「浏览器验收不触发现取」的要求。
- 处理：在临时库补齐 09-25 所有板块的本地成员快照后**重新执行**；最终运行的服务日志
  无任何开盘红/外部取数记录。
- 新增 `/api/sectors/theme-search` 与 `/api/stock/{code}/research-context` 从未调用采集器；
  有测试注入 `_board_members=raise` 与 `DailyCollector=raise` 断言。

### E3. 只读与安全闸门

- 三个库 `PRAGMA integrity_check` 均为 `ok`；mtime/大小与 S2 一致：
  - `fupan.db` 2026-10-31 22:26:44 / 513,519,616B
  - `b1_research.db` 2026-11-09 12:23:12 / 1,441,378,304B
  - `forward_research.db` 2026-11-11 17:17:58 / 67,055,616B
- C0 五个脚本 blob 未变；冻结文件 SHA256 未变（见第 G 节）。
- 未启动调度、未部署、未合并 master、未真实交易。
- 新接口只有 SELECT；`/dde?live=0` 路径未变。

---

## F. 测试与构建（真实退出码）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 | `.\.venv\Scripts\python.exe -m pytest -q` | **1422 passed, 3 skipped**（408.31s） |
| 后端静态 | `.\.venv\Scripts\python.exe -m ruff check backend scripts tests` | **All checks passed** |
| 前端测试 | `npm test` | **131 passed / 12 files** |
| 前端 lint | `npm run lint` | **0 warnings / 0 errors**（126 files） |
| 前端构建 | `npm run build` | **成功**（Vite，771ms） |
| 环境 | Python 3.13.13、Node 24.19.0、Chrome 157 | — |

新增测试：
- `tests/test_theme_intersection.py`（31）
- `tests/test_sentiment_next_day.py`（新增 4）
- `tests/test_stock_research_context.py`（12）
- `tests/test_s3_integration.py`（4 套合成数据集）
- 前端 `StockResearchEvidence.test.tsx`（4）、`StockResearch.test.tsx`（19）、
  `ThemeIntersection.test.tsx`（9）、`SentimentNextDay.test.tsx`（9）、
  `StockResearchDrawer.test.tsx`（16）

---

## G. C0 与冻结文件

| 文件 | Git blob |
| --- | --- |
| `scripts/forward_collect_tushare.py` | `1408f9e3253f0c353023863821ad70544f0fdf2a` |
| `scripts/forward_store.py` | `8064d6709a99b07e853c09c297bf26ad938fab5f` |
| `scripts/forward_track_c0.py` | `6ff3eb0861cb52580c2e20be2be778cf5bfb9abe` |
| `scripts/forward_universe.py` | `2f8979d299f2537427e3ab3c3031a2cb14d22ca0` |
| `backend/app/services/factor_score.py` | `32b6f2ff18373ece44f278a6e76db988385be80f` |

| 冻结文件 | SHA256 |
| --- | --- |
| `signals_2026-11-11.jsonl` | `1F80D1B6785B930FC9A922F027A323A36F5DDCF122D2EC865810F33523F07B37` |
| `signals_2026-11-11.meta.json` | `44301EB5E00867E4E19D51C7DEA524AF0897CED0E7A5002CAC2F530E3FA2DD50` |

---

## H. 接口示例（合成数据，已脱敏）

`GET /api/sectors/theme-search?sector_codes=A01,A02&mode=AND&as_of=2026-09-24`

```json
{
  "requested_as_of": "2026-09-24",
  "resolved_as_of": "2026-09-24",
  "is_exact_date": true,
  "known_by_observation_time": false,
  "historical_signal_eligible": false,
  "quality_version": "theme-quality-v2",
  "taxonomy": "kph_selected",
  "quality": {"status": "PARTIAL", "version": "theme-quality-v2", "reasons": ["OBSERVED_INTERSECTION_ONLY", "NO_MEMBERS_COMPLETION_LOG"]},
  "coverage": {
    "observed_sector_count": 3,
    "expected_sector_count": 10,
    "selected_sector_count": 2,
    "selected_sector_observed": 2,
    "has_completion_proof": false,
    "has_completeness_evidence": false,
    "coverage_status": "PARTIAL_OBSERVED_COUNT",
    "coverage_unknown": false
  },
  "total": 2,
  "shown": 2,
  "has_more": false,
  "items": [{"code": "600000", "name": "合成银行", "match_count": 2, "sectors": [{"code": "A01", "name": "题材1"}, {"code": "A02", "name": "题材2"}], "trade_date": "2026-09-24", "limit_up_consecutive": 2}]
}
```

`GET /api/stock/600000/research-context?as_of=2026-09-24`

```json
{
  "code": "600000",
  "resolved_trade_date": "2026-09-24",
  "data_as_of": "2026-09-25",
  "historical_signal_eligible": false,
  "quality": {"status": "PARTIAL", "reasons": ["THEME_PIT_UNKNOWN", "CONCEPT_POST_CLOSE_UNKNOWN"]},
  "as_known_on_day": {
    "previous_close_date": "2026-09-23",
    "limit_status": "UP",
    "board_height": 2,
    "observed_pool_membership": ["up"],
    "daily": {"trade_date": "2026-09-24", "close": 10.0, "pct_chg": 10.0, "known_by": "CLOSE"},
    "themes": [{"name": "题材1", "sector_code": "A01", "taxonomy": "kph_selected", "fetched_at": "2026-09-25T18:00:00", "known_by": "LATER"}]
  },
  "later_outcome": {"outcome_date": "2026-09-25", "status": "VERIFIED", "pct_chg": -5.0, "limit_up_again": false, "known_by": "FORWARD"},
  "evidence": []
}
```

---

## I. 未验证边界与保留状态

- `REAL_AUCTION_DATA_NOT_YET_VERIFIED`：未做任何 09:15–09:31 真实采集；生产库
  `auction_snapshot` 仍为 0。
- `THEME_REAL_DATA_ACCEPTANCE_PENDING`：`sector_member` 没有成员级完成日志；
  生产副本仅 19/270 板块。本轮不通过现取补数据。
- `PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED`、`P13_RELEASE_NO_GO`、`HISTORICAL_DATA_GAP_UNDER_REVIEW`
  继续保留。
- 真实线上浏览器、真实账户、真实外部行情均未验收。

---

## J. 未通过项 / 已知风险

| 级别 | 事项 | 处置 |
| --- | --- | --- |
| P2 | 首次浏览器脚本触发 `/sectors/members` 现取尝试（errcode=1020） | 已补齐临时库成员快照并重跑，最终运行无外部请求；新接口本身无采集路径 |
| P2 | 题材成员完整性仍无完成协议 | `has_completion_proof=false`，结果最高 `PARTIAL`，留待未来完成协议 |
| P2 | 断板 250 日 2.5s / 300 SQL | 达到目标，但仍有 300 条；如未来需要更低延迟可继续合并日线查询 |
| P2 | 侧栏历史题材无法证明盘前已知 | 标 `LATER/SAME_DAY_UNKNOWN`，`historical_signal_eligible=false` |
| — | 真实竞价 / 生产题材覆盖 / 部署 | 未授权、未执行 |

**S3 结论**：A/B/C/D 代码、测试与受控浏览器均完成并通过；没有采集/发布副作用。
可报告 `S3_INTEGRATED_CODE_READY_FOR_REVIEW`、`THEME_COVERAGE_SEMANTICS_CORRECTED`、
`SENTIMENT_BATCHED_EQUIVALENCE_VERIFIED`、`STOCK_RESEARCH_EVIDENCE_READY`、
`S3_BROWSER_LOCAL_ACCEPTANCE_PASSED`，等待集中独立审查。S3 完成后停止。
