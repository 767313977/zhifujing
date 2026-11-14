# 致富经 S2 大批次验收报告

- 文档版本：S2-acceptance-v1.0
- 编写日期：2026-11-14（业务时间 `Asia/Shanghai`）
- 分支：`feature/stock-screening-v2`（未合并 master、未部署）
- S2 基线：`251cac4c1eadd3e801c798bb3b1180202b05562d`
- S2 代码终稿：`27c7228f7df0a90000fe32bea8f8cddcca271b2b`（本报告提交为其子提交）
- 状态：`S2_IMPLEMENTED_PENDING_INDEPENDENT_REVIEW`

---

## 1. 基线、提交与工作区

### 1.1 Gate 0 实测

| 项目 | 实测值 |
| --- | --- |
| 起始 HEAD | `251cac4c1eadd3e801c798bb3b1180202b05562d` |
| 分支 | `feature/stock-screening-v2` |
| 起始工作区 | clean（`git status --porcelain` 0 行） |
| `origin/master` | `44f0912d9f754e253b06e1296cb1f8b381f158a0`（未动） |
| `git diff --check` | 无输出 |
| `AGENTS.md` | 仓库不存在该文件；以 `.github/workflows/ci.yml` 与现有工程注释为准 |

### 1.2 S2 提交

| 包 | 提交 | 内容 |
| --- | --- | --- |
| A | `c5da915` | `fix(sentiment): preserve valid-sample denominators and explicit aggregation` |
| B | `e6ea702` | `fix(research): enforce as-of display and logout request epochs` |
| C | `9609de2` | `feat(sectors): add provenance-aware theme intersection search` |
| D | `27c7228` | `test(s2): add browser and cross-module acceptance evidence` |
| 报告 | 本文件提交 | `docs(s2): add acceptance report` |

### 1.3 diff 文件清单（`251cac4..27c7228`）

业务与测试 24 个文件、证据 9 个文件，共 31 个文件；**没有数据库文件、没有 C0/冻结文件**。

```
backend/app/api/sector.py
backend/app/schemas.py
backend/app/services/sentiment_next_day.py
backend/app/services/theme_intersection.py            (新增)
frontend/src/api/client.ts
frontend/src/api/types.ts
frontend/src/components/AuctionAnomaly.tsx
frontend/src/components/SentimentNextDay.tsx
frontend/src/components/StockResearchDrawer.tsx
frontend/src/components/ThemeIntersection.tsx         (新增)
frontend/src/lib/StockResearchProvider.tsx
frontend/src/lib/stockResearchCache.ts
frontend/src/lib/stockResearchContext.ts
frontend/src/pages/Sectors.tsx
frontend/src/pages/__tests__/AuctionAnomaly.test.tsx
frontend/src/pages/__tests__/SentimentNextDay.test.tsx
frontend/src/pages/__tests__/StockResearch.test.tsx
frontend/src/pages/__tests__/StockResearchDrawer.test.tsx
frontend/src/pages/__tests__/ThemeIntersection.test.tsx (新增)
reports/s2_auction_sim/auction_sim.{json,md}
reports/s2_browser/*.png（6 张）
scripts/simulate_auction_day.py
tests/test_sentiment_next_day.py
tests/test_theme_intersection.py                      (新增)
```

---

## 2. 各包结果与闸门

| 包 | 结果 | 闸门 |
| --- | --- | --- |
| A 情绪次日统计 | 完成 | `S2_A_STATS_VERIFIED` |
| B 研究侧栏时点/缓存/隔离 | 完成 | `S2_B_RESEARCH_VERIFIED` |
| C 题材 AND/OR 交叉检索 | 完成，代码与合成/真实只读案例通过 | `S2_C_THEME_SEARCH_READY` + `THEME_REAL_DATA_ACCEPTANCE_PENDING`（生产库覆盖仅 19/270 板块） |
| D 浏览器 + 回归 + 报告 | 完成（浏览器真实执行） | `S2_D_INTEGRATION_VERIFIED` |

仍保持：`P0_A_CODE_READY_FOR_LIVE_ACCEPTANCE`、`REAL_AUCTION_DATA_NOT_YET_VERIFIED`、
`PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED`、`P13_RELEASE_NO_GO`、`HISTORICAL_DATA_GAP_UNDER_REVIEW`。

---

## 3. 五项重点问题：复现、修复与测试

### 3.1 S2-P1-01 再涨停分子/分母错位

- **位置**：`backend/app/services/sentiment_next_day.py::_stats`
- **RED 复现**：合成 D 日候选 A/B/C，D+1 只有 A/B 有日线，D+1 涨停池含 B/C。
  - 旧实现：`again=2`、`valid=2` → 比例 100%，`not_limit_up=0`；
    极端场景（候选 2 只、仅 A 有日线、池含 A/B）：`again=2`、`valid=1` → **比例 200%、not_limit_up=-1**。
  - 失败断言：`assert 2 == 1`（`test_limit_up_again_uses_valid_sample_denominator`、
    `test_limit_up_again_extreme_case_never_exceeds_100`）。
- **修复**：只在 `valid_codes`（有 D+1 有效涨跌幅的样本）里统计再次涨停；`not_again = valid - again`。
- **修复后**：`again=1`、`valid=2`、比例 50%；极端场景 `again=1`、`valid=1`、比例 100%。
- **测试**：
  - `tests/test_sentiment_next_day.py::test_limit_up_again_uses_valid_sample_denominator`
  - `::test_limit_up_again_extreme_case_never_exceeds_100`
  - `::test_summary_keeps_sample_conservation_for_every_row`
- **不变量**（每行强制断言）：`sample = valid + missing`；`0 <= again <= valid`；
  `again + not_again = valid`；`0 <= ratio <= 100`（非 null 时）。

### 3.2 S2-P1-02 跨日均值/中位数口径

- **位置**：`sentiment_next_day.py::_summary`、`_bands`
- **RED 复现**：D1 一只 `+10%`、D2 九只各 `-10%`。
  - 旧实现输出 `avg_pct = 0%`（每日均值再平均）、`median_pct` 为每日中位数的中位数；
  - 新字段不存在 → `KeyError: 'security_weighted_avg_pct'`。
- **修复**：
  - 新增 `security_weighted_avg_pct`（逐券 pooled，每个证券-交易日一个观测）；
  - 新增 `day_equal_weight_avg_pct`（交易日等权，保留 S1 旧口径）；
  - `avg_pct` 改为 pooled（与字段文案一致），`median_pct` 改为 pooled 中位数；
  - 顶层新增 `aggregation_version = "next-day-aggregation-v2"`。
- **修复后**：pooled `-8.0%`、日等权 `0.0%`、pooled 中位 `-10.0%`；档位表同样按 pooled。
- **测试**：`tests/test_sentiment_next_day.py::test_summary_uses_pooled_security_observations_not_day_averages`
- **前端**：`SentimentNextDay.tsx` 同时显示「D+1 逐券均值 / 合并中位」「日等权均值」，并带
  「有效证券-交易日数 / 有统计日数」tooltip；底注显示聚合版本。
- **旧字段兼容**：`avg_pct` 仍存在，但语义升级为 pooled；旧口径保留在 `day_equal_weight_avg_pct`，
  不允许再用 `avg_pct` 冒充日等权。

### 3.3 S2-P1-03 侧栏 as-of 语义

- **位置**：`frontend/src/components/StockResearchDrawer.tsx`、`lib/stockResearchContext.ts`
- **旧行为**：`asOfDate` 直接进入缓存键并显示为「数据日 YYYY-MM-DD」，但 `stockProfile/themes/daily`
  取的都是最新资料，历史点击会被误标成历史时点。
- **修复**：
  - `ResearchSelection.asOfDate` 重命名为 `selectedAsOfDate`（来源页观察日）；
  - 标题改为「来源观察日 X」+「此页展示最新本地资料（截至 Y），不代表所选历史日当时已知」；
  - 概览 / K 线 / 题材各自用 API 返回的真实日期显示「截至」；
  - 题材无历史快照时显式显示「历史题材缺失」，不用今日题材推演往日。
- **浏览器实测证据**：抽屉显示「来源观察日 2026-09-24」「最新本地资料（截至 2026-10-09）」，
  见 `reports/s2_browser/research_drawer_1280x800.png`。
- **测试**：
  - `frontend/src/pages/__tests__/StockResearchDrawer.test.tsx::historical observation date is not printed as the data date`
  - `::themes tab says historical snapshot is missing instead of faking it`
  - 说明：B 的新用例与修复同批加入，未单独保留旧实现运行日志；旧实现对应「`数据日 ${asOfDate}`」
    与 `api.stockProfile(code)` 的无日期调用，可静态复核。

### 3.4 S2-P1-04 注销后在途请求回填缓存

- **位置**：`frontend/src/lib/stockResearchCache.ts`
- **旧行为**：`clearResearchCache()` 清 Map，但在途 `loader` 成功后仍会 `writeResearchCache()`；
  旧 `finally` 也可能删掉新请求。
- **修复**：引入单调递增 `epoch`：
  - `loadCached` 捕获发起时 epoch，写缓存前比对，过期则丢弃；
  - `inflight` 记录 `{epoch, promise}`，同 key 只复用**同代次**请求；
  - `finally` 只在当前条目仍属于自己时删除；
  - 登出/换账户调用 `clearResearchCache()` 推进 epoch。
- **保留行为**：失败不缓存；401/403 清旧值；TTL 3 分钟；上限 240 条。
- **测试**：
  - `frontend/src/pages/__tests__/StockResearch.test.tsx::a late response after logout never repopulates the cache`
  - `::an old finally cannot remove the new epoch inflight request`
  - `::switching account clears the selection and the cache`
  - `::logged-out users do not inherit ?stock= from the URL`

### 3.5 S2-P2-01 竞价 5000→1000 与 illegal_values 协议

- **协议锁定**（源码 `auction_anomaly.py::assess_day_quality`）：
  日期级 `VALID` 只表示**帧日志/覆盖证据成功**（有快照、有 0926 与 0931 价格、无帧级 reason），
  **不要求每只证券的业务字段都有效**。逐券有效性由 `assess_stock_quality` 独立判定。
- **修复**：
  - UI 标签由「完整可信」改为 **「帧级完整」**，并加 title 说明「不代表每只证券字段有效」；
  - 顶部新增 `显示 X / 共 Y 条（上限 Z）`，`normal_day_5000` 显示 **1000 / 5000（上限 1000）**，
    不再把接口上限误读成总量；
  - 仿真报告新增「口径说明」，并把异动接口写成 `shown / total（limit，truncated）`。
- **证据**：
  - `reports/s2_auction_sim/auction_sim.md::normal_day_5000`：旧接口 5000；异动 `1000 / 5000（truncated=是）`。
  - `reports/s2_auction_sim/auction_sim.md::illegal_values`：日期 `VALID`，逐券竞价有效 `97/100`，
    口径说明已写清。
  - 前端测试 `AuctionAnomaly.test.tsx::date VALID is labelled as frame-level and truncation shows total/shown`。

---

## 4. 情绪统计口径与对账

### 4.1 两套均值口径（`next-day-aggregation-v2`）

| 字段 | 分母 | 含义 |
| --- | --- | --- |
| `security_weighted_avg_pct` / `avg_pct` | 全部有效**证券-交易日观测** | 逐券 pooled |
| `day_equal_weight_avg_pct` | 有统计的**交易日** | 每日均值再平均（S1 旧口径） |
| `median_pct` | 全部有效观测 | pooled 中位数（不是每日中位数的中位数） |

单个证券在不同 D 日出现算两个观测，不按证券去重。

### 4.2 真实历史只读重算（`b1_research.db`，只读，60 日，结束 2026-09-24）

| 分组 | 有统计日 | 有效观测 | S1 日等权均值 | S2 逐券 pooled | S1 中位数的中位数 | S2 pooled 中位 |
| --- | --- | --- | --- | --- | --- | --- |
| 首板 | 60 | 3,346 | +1.01% | **+0.94%** | +0.27% | **+0.16%** |
| 全部涨停 | 60 | 4,107 | +1.27% | **+1.27%** | +0.20% | **+0.46%** |

首板均值差异来自样本量悬殊的交易日；全部涨停恰好在本次窗口一致，但中位数仍不同。
差异属于**口径差异**，没有修改任何真实数据去「凑近」。

### 4.3 六个真实交易日（首板，只读）

| D 日 | D+1 | 状态 | 样本 | 有效 | 平均 | 中位 | 再涨停比例 | 原因 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-09-17 | 2026-09-18 | VERIFIED | 38 | 38 | +2.52% | +1.29% | 21.05% | — |
| 2026-09-18 | 2026-09-21 | VERIFIED | 66 | 66 | +2.73% | +1.41% | 19.70% | — |
| 2026-09-21 | 2026-09-22 | PARTIAL | 82 | 81 | +2.68% | +1.37% | 20.99% | 1 只缺 D+1 日线 |
| 2026-09-22 | 2026-09-23 | VERIFIED | 39 | 39 | −1.47% | −2.68% | 7.69% | — |
| 2026-09-23 | 2026-09-24 | VERIFIED | 39 | 39 | +0.29% | −0.06% | 20.51% | — |
| 2026-09-24 | 2026-09-28 | PARTIAL | 39 | 39 | −2.37% | −3.98% | null | 09-28 涨停池未采集 |

### 4.4 情绪分档对账（首板，60 日，`sentiment-bands-v1`）

| 档位 | 交易日 | 有统计日 | 有效观测 | 逐券 pooled 均值 | 日等权均值 | pooled 中位 | 上涨比例 | 再涨停比例 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| <30 家 | 1 | 1 | 18 | +4.65% | +4.65% | +6.48% | 77.78% | 22.22% |
| 30–60 家 | 25 | 25 | 873 | +1.09% | +1.16% | +0.48% | 52.46% | 16.79% |
| 60–100 家 | 26 | 26 | 1,686 | +0.53% | +0.57% | −0.32% | 47.15% | 13.35% |
| ≥100 家 | 8 | 8 | 769 | +1.56% | +1.51% | +1.04% | 56.05% | 13.65% |

档位落在同一档内的多日样本按 pooled 汇总；`<30 家` 只有 1 天，两个均值必然相同。
本表只做历史描述，不据此反向优化阈值。

### 4.5 与既有指标交叉核对（全部涨停）

| D → D+1 | S2 `avg_pct` | `market_sentiment.yesterday_limit_today_avg` | 差异 |
| --- | --- | --- | --- |
| 09-17 → 09-18 | 2.78 | 2.78 | 0 |
| 09-18 → 09-21 | 3.23 | 3.23 | 0 |
| 09-21 → 09-22 | 2.60 | 2.57 | 0.03（该日 1 只缺日线） |
| 09-22 → 09-23 | −0.37 | −0.37 | 0 |
| 09-23 → 09-24 | 0.55 | 0.55 | 0 |
| 09-24 → 09-28 | −1.95 | null | 既有指标未采集 09-28 |

`first_board_track` / `limit_funnel` 口径不同（前者是当日首板，后者是上一交易日连板梯队的晋级），
本报告只做**交叉核对**，没有为对齐而改旧指标：
- 09-23 → 09-24：`limit_funnel` `VERIFIED`，首板基数 39；
- 09-24 → 09-28：`UNVERIFIED`（`TODAY_UP_POOL_MISSING`、`TODAY_BROKEN_POOL_MISSING`）。

---

## 5. 研究侧栏时点、缓存与账户隔离

| 场景 | 结果 | 证据 |
| --- | --- | --- |
| 历史观察日 vs 最新资料 | 分开显示，不把 selected 印成数据日 | 浏览器截图 + 抽屉用例 |
| 同代码不同观察日 | 缓存键不同，不混用 | `changing the data date refetches…` |
| 同代码同观察日重复打开 | 3 分钟内复用，标题+概览合并成 1 次请求 | `reopening the same stock reuses the short-term cache` |
| 快速 A→B | 旧响应不覆盖新股票 | `a slow previous stock cannot override…` |
| 登出晚到响应 | 不写缓存（cache size 0） | `a late response after logout…` |
| 旧 finally | 不删新代次 inflight | `an old finally cannot remove…` |
| 切换账户 | 清选择与缓存 | `switching account clears…` |
| 未登录 `?stock=` | 清 URL，不打开侧栏 | `logged-out users do not inherit…` |
| Esc / 焦点 | 打开聚焦关闭按钮，Esc 关闭并还焦点 | `focus moves into the drawer…` |
| 打开侧栏副作用 | 不调用 `syncStock`，资金 Tab 用 `live=0` | `the drawer never triggers a history sync` |

`accountScope` 仍是 djb2 非加密散列，只做同账户窗口比对；真正访问控制依赖后端登录鉴权（已在代码注释写明）。

---

## 6. 题材交叉检索（v1）

### 6.1 接口与数据边界

- 路由：`GET /api/sectors/theme-search`（前缀 `/api/sectors`，需登录 `current_user`）
- 服务：`backend/app/services/theme_intersection.py`
- 数据：仅本地 `sector_member` 快照；`stock_concept` 只覆盖涨停股，**不参与全市场成员交集**
- 参数：`sector_codes`（1~10）、`mode=AND|OR`、`market=all|sh|sz|bj`、`as_of`、`limit(<=500)`、`offset`
- 返回：`requested_as_of` / `resolved_as_of` / `source_snapshot_date` / `is_exact_date`、
  `taxonomy`、`selected_sectors`（含 `member_count` / `has_data`）、`coverage`、
  `total/shown/has_more`、`quality.status/reasons`、`warnings`、逐股命中来源与涨停标记。

### 6.2 真实历史只读案例（`b1_research.db`，2026-09-24，260/270 板块）

| 案例 | 模式 | 命中总数 | 查询数 | 耗时 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 芯片(801001) × 算力(801807) | AND | 362 | 7 | 69.9ms | PARTIAL |
| 芯片 + 算力 | OR | 1,806 | 7 | 41.1ms | PARTIAL |
| 芯片 × 算力 × 人工智能(801085) | AND | 221 | 7 | 44.6ms | PARTIAL |
| 芯片 + 算力 + 人工智能 | OR | 2,537 | 7 | 37.3ms | PARTIAL |
| 10 个板块 | OR | 4,811 | 7 | 88.8ms | PARTIAL |
| 10 个板块 | AND | 0 | 6 | 61.3ms | PARTIAL |

覆盖：AND 2 的 `sample_rows=2168`（所选板块）；研究库全市场 `5869` 只 / `260` 板块，
`expected_sectors=270`，`has_completeness_evidence=false`，因此一律带
`NO_MEMBERS_COMPLETION_LOG` / `SNAPSHOT_SECTOR_COVERAGE_LOW`，不宣称全市场完整。
浏览器验收用的生产库副本当天只有 `2880` 只 / `19` 板块（`1962` 行），同样标 `PARTIAL`。

### 6.3 质量规则

- AND 任一板块当天无成员快照 → `UNVERIFIABLE` + 空结果 + `AND_UNVERIFIABLE`，**不返回误导性的空交集**；
- OR 缺覆盖 → `PARTIAL`，保留可用部分；
- `as_of` 只向过去解析（`is_exact_date=false` 标出回落），不使用未来快照；
- 同名不同代码保留各自 `sector_code`，不静默合并；跨 taxonomy 标 `TAXONOMY_MIXED`；
- 分页稳定按代码升序，`limit` 上限 500。

### 6.4 前端

`frontend/src/components/ThemeIntersection.tsx` 已接入 `Sectors.tsx` 底部整行宽区块：
可搜索题材、多选 chips、AND/OR、市场过滤、快照日、覆盖/来源/质量提示、分页、
`StockLink` 跳转与「研」快速研究。窄屏与 4K 均已在浏览器实测。

---

## 7. 浏览器实测（真实执行，不是 NOT_RUN）

- 环境：Chrome **157.0.8103.79** headless（CDP `--headless=new`，端口 9223）
- 服务：`uvicorn app.main:app`，临时库 `%TEMP%\s2_e2e\e2e.db`（从 `backend/data/fupan.db` **只读复制**近月数据）
- 安全开关：`SCHEDULER_ENABLED=false`（启动日志“定时采集已关闭”）、`WARMUP_PATTERN_CACHE=false`
- 账户：临时库内 `s2tester`（非真实账号）
- 通过项：
  1. 情绪面板（`/sentiment`）：逐券均值 / 日等权 / 聚合版本；
  2. 题材交叉（`/sectors`）：机器人概念 + 医药 AND，64 只，`PARTIAL` 与覆盖提示；
  3. 快速研究抽屉：显示「来源观察日 2026-09-24 / 最新本地资料（截至 2026-10-09）」；
  4. Esc 关闭抽屉；
  5. 涨停复盘原有 `StockLink`：`href=/stock/601811`；
  6. 浏览器控制台 0 个 error。
- 截图（`reports/s2_browser/`）：
  - `sentiment_1280x800.png`、`sentiment_1920x1080.png`、`sentiment_3840x2160.png`
  - `theme_intersection_1280x800.png`、`theme_intersection_3840x2160.png`
  - `research_drawer_1280x800.png`
  - `limit_review_1280x800.png`
- 局限：只覆盖本地临时库与本地服务，**不是** zhifujing.top 真实账号/线上数据验收；
  未做移动端真机、Safari/Firefox 交叉浏览器。

---

## 8. 竞价离线仿真与真实状态

- 复跑：`.\.venv\Scripts\python.exe scripts\simulate_auction_day.py --out reports\s2_auction_sim`
- 结果：**21/21 PASS**（当前 HEAD `27c7228`，与上传的 S1 报告场景数一致）
- 新增口径说明：
  - `VALID` = 帧/覆盖证据成功，不等于逐券字段全有效；
  - `normal_day_5000`：旧接口 5000；异动接口 **1000 / 5000（limit=1000，truncated=是）**；
  - `illegal_values`：日期 `VALID`，逐券竞价有效 `97/100`，属逐券清洗，不降级日期质量。
- 真实生产库只读检查：
  - `auction_snapshot` = **0 行**；
  - 竞价帧日志（`collect_log` 中 auction 任务）= **0 行**；
  - 生产库不存在独立的 `auction_frame` 表（当前 Schema 只有 `auction_snapshot`）。
- 结论：`REAL_AUCTION_DATA_NOT_YET_VERIFIED`；本任务没有执行任何 09:15–09:31 真实采集。

---

## 9. 全量测试与工程检查

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 | `.\.venv\Scripts\python.exe -m pytest -q` | **1396 passed, 3 skipped**（351.90s，4 个既有 DeprecationWarning） |
| 后端静态 | `.\.venv\Scripts\python.exe -m ruff check backend scripts tests` | **All checks passed** |
| 前端测试 | `npm test` | **124 passed / 11 files** |
| 前端 lint | `npm run lint` | **0 warnings / 0 errors**（125 files） |
| 前端构建 | `npm run build` | **成功**（Vite，560ms） |
| 环境 | Python 3.13.13、Node 24.19.0、npm 11.17.0、Chrome 157 | — |

没有删测试、没有新增 skip、没有放宽安全阈值。

---

## 10. 跨模块契约与权限

1. 涨停复盘：浏览器 `/limit-up` 正常渲染，`StockLink` 指向 `/stock/{code}`；组件测试通过。
2. P0-B 晋级漏斗：09-23→09-24 `VERIFIED`（首板基数 39）；09-24→09-28 `UNVERIFIED`（当日池缺失）；
   原 `/limit/promotion` 未改动。
3. 竞价强度旧接口：未改动；`premarket` / `auction` 测试通过。
4. 竞价异动 API：未改接口字段，仅前端标签与 `total/shown` 展示。
5. 情绪原有 12 项：页面正常渲染，`Sentiment` 相关测试通过。
6. 个股详情 / StockLink：跳转 href 与前进后退语义未改。
7. 自选：`WatchlistProvider` 测试通过；侧栏只读展示自选状态。
8. 登录/登出：`auth` 相关测试通过；未登录 `?stock=` 会被清理。
9. 新 API 鉴权：`/api/sentiment/next-day` 在 app 级登录关卡内；
   `/api/sectors/theme-search` 显式依赖 `current_user`（有路由依赖测试）。
10. 新组件未绕过权限：侧栏未登录时只显示「登录后才能查看」，不请求个股数据。
11. C0 与冻结文件未变（见第 12 节）。
12. 后端启动：未新增 Schema 迁移；临时库用 `init_db()` 初始化，服务以 `FUPAN_SCHEMA_MODE=verify` 启动，
    日志确认「未执行任何 DDL」。

只读副作用审计：
- `sentiment_next_day.py` / `theme_intersection.py` 仅 `select`；
- 侧栏使用的 `/{code}/profile`、`/daily`、`/themes`、`/limit-history`、`/lhb` 均为只读；
- 资金 Tab 调 `/{code}/dde?live=0`，不触发现取；
- 侧栏不调用 `POST /{code}/sync`（单测断言 `syncStock` 未被调用 + 浏览器实测）。

---

## 11. 性能与数据库状态

### 11.1 `/api/sentiment/next-day`（只读，`b1_research.db`，结束 2026-09-24）

| 窗口 | 分组 | 耗时 | SQL 数 | 行数 | 有效观测 |
| --- | --- | --- | --- | --- | --- |
| 20 | 首板 | 51.1ms | 25 | 20 | 978 |
| 20 | 全部涨停 | 79.3ms | 25 | 20 | 1,244 |
| 20 | 断板 | 172.3ms | 185 | 20 | 901 |
| 60 | 首板 | 112.9ms | 65 | 60 | 3,346 |
| 60 | 全部涨停 | 124.0ms | 65 | 60 | 4,107 |
| 60 | 断板 | 667.5ms | 545 | 60 | 3,143 |
| 120 | 首板 | 437.1ms | 125 | 120 | 7,509 |
| 120 | 全部涨停 | 298.8ms | 125 | 120 | 9,107 |
| 120 | 断板 | 1,325.9ms | 1,085 | 120 | 6,983 |
| 250 | 首板 | 474.4ms | 255 | 250 | 13,975 |
| 250 | 全部涨停 | 474.0ms | 255 | 250 | 17,495 |
| 250 | 断板 | 2,520.7ms | 2,255 | 250 | 12,874 |

250 日全部涨停峰值内存 **13.94MB**。断板的查询数随天数线性增长（每日复用 P0-B
`load_funnel_inputs` 的多条查询），当前 250 日 2.5s 在可接受范围；这是后续可优化项，
没有改动正确性定义。

### 11.2 题材交叉

2/3/10 板块 AND/OR 均为 **6~7 条 SQL、37~89ms**（1,962~12,996 行成员），
不随成员数增长；无索引缺失导致的额外扫描（`sector_member` 主键含 `trade_date + sector_code`）。

### 11.3 研究侧栏

- 同 key 并发 5 次只加载 1 次（既有测试）；
- 标题 + 概览共用一份 profile；
- 缓存 TTL 3 分钟、上限 240、401/403 不缓存；
- 10 只股票快速切换时旧响应被序号丢弃，实际请求数 = 每个 Tab 每只股票 1 次（无重复）。

### 11.4 数据库只读

| 文件 | 大小 | 最后修改 | 本轮操作 |
| --- | --- | --- | --- |
| `backend/data/fupan.db` | 513,519,616 | 2026-10-31 22:26:44 | 只读复制到临时库 |
| `backend/data/forward_research.db` | 67,055,616 | 2026-11-11 17:17:58 | 未打开 |
| `backend/data/b1_research.db` | 1,441,378,304 | 2026-11-09 12:23:12 | `mode=ro` 只读 |

临时验收库：`%TEMP%\s2_e2e\e2e.db`，验收后未进入仓库；浏览器截图只含临时账户 `s2tester`。

### 11.5 外部网络与写入证据

- 后端全量测试使用临时 SQLite + mock，未请求腾讯 / Tushare / iFinD；
- 浏览器验收服务启动时 `SCHEDULER_ENABLED=false`，日志明确「定时采集已关闭」；
- 临时库由 `backend/data/fupan.db` **只读 ATTACH 复制**，生产库文件 mtime 未变化；
- 新服务（`sentiment_next_day`、`theme_intersection`）只有 `SELECT`；
- 没有新增定时任务、没有触发 `collect_auction` / `sync_stock` / 外部行情抓取。

---

## 12. C0 / 前瞻 / 冻结文件复核

| 文件 | Git blob | 与 S2 基线 |
| --- | --- | --- |
| `scripts/forward_collect_tushare.py` | `1408f9e3253f0c353023863821ad70544f0fdf2a` | 未变 |
| `scripts/forward_store.py` | `8064d6709a99b07e853c09c297bf26ad938fab5f` | 未变 |
| `scripts/forward_track_c0.py` | `6ff3eb0861cb52580c2e20be2be778cf5bfb9abe` | 未变 |
| `scripts/forward_universe.py` | `2f8979d299f2537427e3ab3c3031a2cb14d22ca0` | 未变 |
| `backend/app/services/factor_score.py` | `32b6f2ff18373ece44f278a6e76db988385be80f` | 未变 |

| 冻结文件 | SHA256 |
| --- | --- |
| `backend/data/forward_c0/signals_2026-11-11.jsonl` | `1F80D1B6785B930FC9A922F027A323A36F5DDCF122D2EC865810F33523F07B37` |
| `backend/data/forward_c0/signals_2026-11-11.meta.json` | `44301EB5E00867E4E19D51C7DEA524AF0897CED0E7A5002CAC2F530E3FA2DD50` |

授权边界：未修改 C0、未合并 master、未部署、未启动生产调度、未写生产库、未执行真实交易。

---

## 13. 未来真实竞价验收所需单独授权清单（只列方案，不执行）

1. 单独书面授权，明确日期与责任人；
2. 环境：隔离 staging 或本地副本，不使用生产库；
3. 时间窗：09:15–09:31（含 0926、0931 帧），提前确认交易日历；
4. 请求：仅腾讯竞价接口，限定证券集合与 QPS，保留请求日志（脱敏）；
5. 写库：只写 staging/临时库，记录写入行数与事务边界；
6. 停止条件：网络错误率、覆盖不足、写库失败、日期异常任一触发即停止；
7. 验收指标：13 帧状态、覆盖率、逐券质量、日期质量、旧/新 API 对账；
8. 验收后只读复核真实库行数与质量，不自动进入 S3。

---

## 14. 回滚计划（未执行）

| 回滚单元 | 命令 | 影响 |
| --- | --- | --- |
| A | `git revert c5da915` | 恢复旧分子/分母与旧聚合口径（不推荐单独回滚 B/C） |
| B | `git revert e6ea702` | 恢复旧 as-of 文案与无 epoch 缓存 |
| C | `git revert 9609de2` | 移除题材交叉路由/组件/测试 |
| D | `git revert 27c7228` | 回退竞价标签、仿真报告与浏览器截图 |

四者相互独立；**本报告不执行任何回滚**。

---

## 15. 风险清单

| 级别 | 事项 | 状态 / 处置 |
| --- | --- | --- |
| P1 | 再涨停分子含缺行情样本 | 已修复 + 不变量测试 |
| P1 | 跨日均值/中位数口径混用 | 已修复 + 双口径字段 + 版本号 |
| P1 | 侧栏历史观察日被误标为数据日 | 已修复 + 浏览器证据 |
| P1 | 注销后在途请求回填缓存 | 已修复 + epoch 测试 |
| P2 | `illegal_values` 日期 VALID / 逐券 97% | 已锁定协议并改 UI 标签 |
| P2 | 5000→1000 被误读为总量 | 已展示 `total/shown/limit` + 仿真报告 |
| P2 | 断板分组 250 日 2.5s / 2255 SQL | 可接受，留待后续批量优化 |
| P2 | 题材成员无完成日志；生产库仅 19/270 板块 | `THEME_REAL_DATA_ACCEPTANCE_PENDING`；AND 缺覆盖返回 `UNVERIFIABLE` |
| P2 | 侧栏没有历史 as_of 快照 | 明确提示「最新资料，不代表历史日」；不伪造历史 |
| — | 真实竞价、线上部署、C0 | 未授权、未执行，状态保持 |

**整批结论**：S2 代码与合成/真实只读证据已交付，等待一次集中独立审查；
不得据此宣称生产发布、真实竞价或线上浏览器验收通过。
