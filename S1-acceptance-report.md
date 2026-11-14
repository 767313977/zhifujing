# 致富经 S1 综合开发批次 · 验收报告

> 批次：S1（A 竞价可靠性收口 / B 情绪次日表现 / C 全站股票研究侧栏 / D 集成验收）
> 分支：`feature/stock-screening-v2`
> 生成日期：2026-11-14（Asia/Shanghai）
> 说明：本报告中的竞价数据**全部来自离线合成仿真**；真实竞价联网采集仍未执行。

---

## A. 基线与代码变更

### A1. Git 基线

| 项目 | 值 |
| --- | --- |
| 初始 HEAD（批次基线） | `bd95fc5d49dd719674b56a2af9855e1a79b26db4` |
| 初始分支 | `feature/stock-screening-v2` |
| 初始工作区 | 干净（`git status --porcelain` 为空） |
| 提交 A（竞价） | `49d1edb` |
| 提交 B（情绪次日） | `87479ff` |
| 提交 C（研究侧栏） | `505aa0d` |
| 提交 C2（侧栏请求去重） | `4d9e868` |
| 提交 D（验收报告 + 测试收口） | `2c86b81` |
| 最终 HEAD（本报告验证的代码） | `2c86b81` |

> 若仓库随后只有一个「补记 SHA」的纯文档提交，最终 `git log -1` 会指向它；被验证的代码与报告内容对应 `2c86b81`。

> 本批次只推送功能分支，未合并 master、未部署。

### A2. 每个工作包的实现说明

#### A. 竞价可靠性（P0-A 收口）

| 文件 | 变更 |
| --- | --- |
| `backend/app/jobs/collect_auction.py` | 逐券时间证据改为「所有已持久化日志取最大源时间」；失败/跳过不再擦除证据；写事务开头 `BEGIN IMMEDIATE` 先取写锁；金额/量/换手负数清洗为 NULL（0 保留） |
| `backend/app/services/auction_anomaly.py` | `_frame_logs` 质量结论以最后一条 `ok/partial`（或 noop）日志为准；新增 `last_attempt_*` 诊断字段与 `warnings`；最新失败不降级已有有效行情 |
| `backend/app/schemas.py` | `AuctionAnomalyFrame` 增加诊断字段；`AuctionAnomalyQuality` 增加 `warnings` |
| `frontend/src/components/AuctionAnomaly.tsx` | 展示诊断提示「最近一次尝试失败（有效数据仍在）」；`partial` 与失败不混淆 |
| `frontend/src/api/types.ts` | 同步新增字段类型 |
| `scripts/simulate_auction_day.py` | 新增 13 帧离线仿真器（独立临时 SQLite、合成 `fetch_spot`） |
| `tests/test_auction_s1.py` | 新增 9 个用例（成功→失败→成功、noop、崩溃回滚、单调性、并发写锁、重复采样、partial 重试补齐） |
| `reports/s1_auction_sim/*` | 仿真输出 JSON + Markdown |

**核心修复**：R1 的 `_previous_frame_quote_times()` 按日志 ID 倒序取**第一条**匹配日志；若最新一条是失败尝试（`quote_times` 为空），会返回空字典，导致第三次合法升级被 `_is_newer_quote(prev=None, has_existing=True)` 拒绝。现在改为遍历所有日志、只采信 `ok/partial/noop` 证据、逐券取最大时间；并在同一事务里先 `BEGIN IMMEDIATE` 再读取，保证跨进程「读旧时间 → 判定 → 写入」不是基于过期快照。

#### B. 情绪 × 次日表现（P1-C）

| 文件 | 变更 |
| --- | --- |
| `backend/app/services/sentiment_next_day.py` | 新增服务：D 日分组、交易日历取 D+1、D+1 统计、质量状态、固定版本情绪分档 |
| `backend/app/api/market.py` | 新增 `GET /api/sentiment/next-day`（`/api/sentiment` 前缀路由） |
| `backend/app/main.py` | 注册 `market.sentiment_router` |
| `backend/app/schemas.py` | 新增 `SentimentNextDayOut` 及子模型 |
| `frontend/src/components/SentimentNextDay.tsx` | 新面板：区间/分组/指标、双轴图、档位表、逐日表、质量说明 |
| `frontend/src/pages/Sentiment.tsx` | 在「连板高度」之后接入新面板 |
| `frontend/src/api/client.ts` / `types.ts` | 新增 `sentimentNextDay` 与响应类型 |
| `tests/test_sentiment_next_day.py` | 12 个用例（分组、PENDING/PARTIAL/UNVERIFIED、断板复用、分档、只读） |
| `frontend/src/pages/__tests__/SentimentNextDay.test.tsx` | 7 个组件测试 |

#### C. 全站股票研究侧栏（P1-B）

| 文件 | 变更 |
| --- | --- |
| `backend/app/api/stock.py` | 新增只读 `GET /api/stock/{code}/limit-history`、`GET /api/stock/{code}/lhb`；`/dde` 增加 `live=false` 只读模式（默认行为不变） |
| `backend/app/schemas.py` | 新增 `StockLimitRecord` |
| `frontend/src/lib/stockResearchContext.ts` | 选择状态/标签页/标准化/默认空实现 |
| `frontend/src/lib/StockResearchProvider.tsx` | URL `?stock=` 双向同步、多窗口 `BroadcastChannel`、账户隔离、登出清理 |
| `frontend/src/lib/stockResearchCache.ts` | 按「账户 + 代码 + 数据日 + 资源」缓存 3 分钟，401/403 不缓存，在途去重 |
| `frontend/src/components/StockResearchDrawer.tsx` | 右侧抽屉：概览 / K线 / 题材 / 涨停 / 资金 / 龙虎榜 |
| `frontend/src/components/StockResearchHost.tsx` | 按需挂载抽屉（ECharts 不进公共 Layout 块） |
| `frontend/src/components/QuickResearch.tsx` | 「研」按钮：`preventDefault + stopPropagation`，不干扰原有导航 |
| 页面接入 | 涨停复盘 `LimitTable`、晋级漏斗 `PromotionFunnelStockTable`、竞价异动 `AuctionAnomaly`、板块成分 `Sectors.MemberTable`、形态选股 `Patterns`、自选 `WatchlistPanel`、选股池 `Pools` |
| `tests/test_stock_research_api.py` | 3 个只读接口用例 |
| `frontend/src/pages/__tests__/StockResearch.test.tsx` | 15 个 Provider/URL/广播/缓存/StockLink 用例 |
| `frontend/src/pages/__tests__/StockResearchDrawer.test.tsx` | 13 个抽屉交互用例 |

### A3. 是否与已有功能冲突

- 原有 `StockLink` 默认点击逻辑**未改**，新增的「研」按钮只打开侧栏；
- 原有列表页 `rememberStockList()` 与个股页 ← → 前后翻未改（`Sectors` / `Pools` / `WatchlistPanel` 只加按钮）；
- 原有竞价强度 Tab、晋级漏斗、情绪 12 项指标未改逻辑；
- `/api/stock/{code}/dde` 默认 `live=true`，老行为不变；新增 `live=false` 仅侧栏使用；
- 无数据库 schema 变更（没有新表/新列），启动不需要迁移。

---

## B. 竞价模块（P0-A 离线可靠性）

### B1. 完整 13 帧离线仿真

工具：`scripts/simulate_auction_day.py`
输出：`reports/s1_auction_sim/auction_sim.json` 与 `reports/s1_auction_sim/auction_sim.md`
结果：**21 / 21 场景通过**（2026-11-14 16:31:35 生成）。

| 场景 | 规模 | 快照行 | 日期质量 | 结论 |
| --- | --- | --- | --- | --- |
| normal_day_100 | 100 | 100 | VALID | 13 帧全 ok，12 个结果帧 + 0931 |
| normal_day_1000 | 1000 | 1000 | VALID | 同上 |
| normal_day_5000 | 5000 | 5000 | VALID | 近全市场规模 |
| partial_batches | 100 | 100 | INCOMPLETE | `failed_batches=2` → partial + BATCH_FAILED |
| late_proven | 100 | 100 | INCOMPLETE | 09:27:10 执行、源报价 09:25:5x 可证明 → 写入 |
| late_unproven | 100 | 0 | INCOMPLETE | 源报价越界 → 整帧跳过，不污染 0926 |
| slow_fetch | 100 | 100 | INCOMPLETE | 耗时被记录，帧仍可写 |
| old_date | 100 | 0 | SOURCE_UNAVAILABLE | 旧日期报价全部丢弃 |
| missing_source_time | 100 | 100 | INCOMPLETE | 5/100 缺源时间（关键帧丢弃）→ 0926 仅 95 只 |
| missing_0926 | 100 | 100 | INCOMPLETE | 过程帧完整但无终值 → `FRAME_MISSING_0926` |
| missing_0931 | 100 | 100 | INCOMPLETE | 竞价值保留、承接为 null（不写 0） |
| pending_before_0931 | 100 | 100 | INCOMPLETE | 9:31 前 → `POST_OPEN_PENDING` / 逐券 PENDING |
| write_rollback | 100 | 100 | INCOMPLETE | 0926 写库抛错 → 整体回滚，保留过程帧 |
| duplicate_requests | 100 | 100 | VALID | 重复请求 noop，不降级质量、不重复样本 |
| concurrent | 1 | 1 | INCOMPLETE | 两线程同帧竞争，较旧报价不覆盖较新报价 |
| interrupt_rerun | 100 | 100 | INCOMPLETE | 中断后重跑可恢复、无重复 0926 |
| low_coverage | 100 | 30 | INCOMPLETE | 请求 100 返回 30、无批次失败 → coverage 0.3/GAP |
| illegal_values | 100 | 100 | VALID | 价格 ≤0 / 负金额 / 负量比被清洗（0 保留） |
| non_trade_day | 100 | 0 | NOT_COLLECTED | 非交易日不采样 |
| no_data | 100 | 0 | NOT_COLLECTED | 空库 → NO_AUCTION_SNAPSHOT |
| success_failed_success | 100 | 100 | INCOMPLETE | 成功→失败→成功：第三次报价 09:26:50 合法升级 |

每个场景都检查了：最终快照、逐券 `samples` 顺序与重复帧、最新合法报价是否被覆盖、`collect_log`、帧覆盖率、日期/逐券质量、新异动 API、旧 `/api/auction` 兼容性。

**注意**：以上均为合成数据，仅验证代码路径；**不代表真实行情验收成功**。

### B2. 成功 → 失败 → 成功恢复证据

单元测试 `tests/test_auction_s1.py::test_success_failed_success_allows_legal_upgrade`：

1. 09:26:20 成功：`quote_time=20261111092618`，价格 10.5；
2. 09:26:40 `fetch_spot` 抛错：只写 failed 日志，不改行情；
3. 09:27:10 迟到候选 0926，`quote_time=20261111092650`；
4. 结果：写入成功，价格 10.6，`samples` 中只有一条 0926，最终 `quote_times={"600000":"092650"}`。

若按 R1 逻辑，第 2 步的失败日志会让第 3 步被误判为「不更新」；本批次修复后不再发生。

### B3. 重复采样时间单调性

- `test_newer_then_older_is_ignored_in_both_orders`：先 09:26:40 后 09:26:01 → 第二次 `NO_NEWER_QUOTE`，价格保持 10.6；
- `test_older_then_newer_upgrades`：先 09:25:59 后 09:26:40 → 允许升级；
- `test_repeated_identical_quotes_never_duplicate_samples`：同时间重复 3 次 → 只有一条 0926；
- `test_partial_then_full_retry_fills_missing_codes`：95 只 partial → 迟到全量重试 → 100 只、日期恢复 `VALID`。

### B4. 并发与崩溃

- `test_write_lock_serializes_other_writers`：持有 `BEGIN IMMEDIATE` 时，另一连接 `BEGIN IMMEDIATE` 立即 `database is locked`，证明写锁生效；
- `tests/test_auction_s1.py::...crash_rollback...`：写库抛错后旧行情保持，随后的更晚报价仍可升级；
- 仿真 `concurrent` 场景：两个线程同时写同一帧，最终始终是源时间更晚的报价；
- 仿真 `interrupt_rerun`：进程中断后重跑，价格更新且 0926 只有一条样本。

### B5. 覆盖率与日志对账

- 100/100 → `coverage_ratio=1.0 / FULL`、`status=ok`；
- 95/100 → `partial`、`coverage_ratio=0.95 / GAP`、`missing_quote_codes=5`；
- 30/100 → `partial`、`0.3 / GAP`、`missing=70`；
- 0931 只更新已存在的 0926 行：`eligible_existing_codes=100`、`returned_codes=50` → `missing=50`；
- 所有 `dropped_*` 列表超过 200 只时截断并置 `*_truncated=true`，计数保持全量。

### B6. 真实竞价验收状态

| 检查 | 结果 |
| --- | --- |
| `backend/data/fupan.db::auction_snapshot` | **0 行** |
| `auction_frame` 采集日志 | **0 条** |
| 真实腾讯请求 | 未执行 |
| 竞价调度 | 未启动/未注册 |

结论：保持 `REAL_AUCTION_DATA_NOT_YET_VERIFIED`，不宣称真实交易日验收通过。

---

## C. 情绪次日表现（P1-C）

### C1. API 示例

`GET /api/sentiment/next-day?days=6&group=first_board&metric=limit_up_count&include_pending=true&date=2026-09-24`

```json
{
  "group": "first_board",
  "group_label": "D 日首板",
  "metric": "limit_up_count",
  "metric_label": "涨停家数",
  "threshold_version": "sentiment-bands-v1",
  "data_as_of": "2026-10-09",
  "summary": {
    "day_count": 6,
    "stat_day_count": 6,
    "sample_count": 303,
    "valid_count": 302,
    "missing_count": 1,
    "avg_pct": 0.73,
    "median_pct": 1.41,
    "up_ratio": 54.3,
    "limit_up_again_ratio": 18.63
  },
  "series": [
    {
      "sample_date": "2026-09-21",
      "outcome_date": "2026-09-22",
      "sample_count": 82,
      "valid_count": 81,
      "missing_count": 1,
      "avg_pct": 2.68,
      "up_ratio": 60.49,
      "limit_up_again_ratio": 20.99,
      "data_quality_status": "PARTIAL",
      "data_as_of": "2026-10-09"
    }
  ]
}
```

### C2. 统计公式

- `avg_pct = mean(D+1 有效 pct_chg)`；`median_pct = median(...)`；
- `up_ratio = D+1 上涨家数 ÷ 有效样本 × 100`；
- `limit_up_again_ratio = D+1 涨停池命中数 ÷ 有效样本 × 100`（D+1 涨停池不可信时为 null）；
- `missing_count = sample_count − valid_count`（停牌/无日线，**不当 0%**）；
- `unverifiable_count`：D+1 涨停池不可信、无法判断是否再涨停的有效样本数；
- `avg_open_pct = mean(D+1 开盘 ÷ D 收盘 − 1)`（同源不复权）；
- `avg_close_vs_open = mean(D+1 收盘 ÷ D+1 开盘 − 1)`；
- 样本集合只来自 **D 日收盘已确定**的池（涨停 / 首板 / 二板 / 三板+ / 炸板 / P0-B FAILED 断板），D+1 只用于统计结果。

### C3. 数据质量口径

| 状态 | 含义 | 数值列 |
| --- | --- | --- |
| `VERIFIED` | D 日源可信、D+1 全部样本有行情、D+1 涨停池可信 | 全部给出 |
| `PARTIAL` | 部分样本缺行情，或 D+1 涨停池不可信 | 均值基于有效样本；再涨停比例可能 null |
| `PENDING` | D+1 行情尚未到齐（`outcome > data_as_of` 或 0 有效样本且未到） | `avg_pct/up_ratio/...` 全为 null |
| `UNVERIFIED` | D 日池未采集/不可信，无法确定样本 | 全为 null |
| `EMPTY` | 该分组当日没有样本（如当天没有三板+） | 全为 null |

### C4. 真实历史交易日只读统计（来源：`backend/data/b1_research.db`，只读打开）

`group=first_board`，`metric=limit_up_count`，窗口截至 2026-09-24：

| D 日 | D+1 | 状态 | 样本 | 有效 | 缺失 | 平均 | 上涨比例 | 再涨停 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-09-17 | 2026-09-18 | VERIFIED | 38 | 38 | 0 | +2.52% | 68.42% | 21.05% |
| 2026-09-18 | 2026-09-21 | VERIFIED | 66 | 66 | 0 | +2.73% | 66.67% | 19.70% |
| 2026-09-21 | 2026-09-22 | PARTIAL | 82 | 81 | 1 | +2.68% | 60.49% | 20.99% |
| 2026-09-22 | 2026-09-23 | VERIFIED | 39 | 39 | 0 | −1.47% | 33.33% | 7.69% |
| 2026-09-23 | 2026-09-24 | VERIFIED | 39 | 39 | 0 | +0.29% | 48.72% | 20.51% |
| 2026-09-24 | 2026-09-28 | PARTIAL | 39 | 39 | 0 | −2.37% | 33.33% | **null** |

说明：

- 09-21 有 1 只样本缺 D+1 日线 → `PARTIAL`，均值只按 81 只算；
- 09-24 的 D+1（09-28）日线存在、但本地 09-28 涨停池未采集 → 再涨停比例 `null`、`unverifiable_count>0`，不会伪报 0%；
- 逐日有效/缺失数量见上表；总体：303 样本 / 302 有效 / 1 缺失。

**PENDING 证据**（不修改真实库）：把上述真实库的相关表复制到临时 SQLite，并把日线截断到 09-24 后查询：

| D 日 | D+1 | 状态 | 样本 | 有效 | 缺失 | 平均 | 上涨比例 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-09-22 | 2026-09-23 | VERIFIED | 39 | 39 | 0 | −1.47% | 33.33% |
| 2026-09-23 | 2026-09-24 | VERIFIED | 39 | 39 | 0 | +0.29% | 48.72% |
| 2026-09-24 | 2026-09-28 | **PENDING** | 39 | 0 | 39 | **null** | **null** |

### C5. 与现有指标对账

1. `yesterday_limit_today_avg`（D 日情绪行里的「昨日涨停股今日均涨幅」） vs 本服务 `all_limit_up`（D−1 样本、D 结果）：

| 日期 | 旧指标 | 本服务 | 差异 |
| --- | --- | --- | --- |
| 2026-09-18 | 2.78 | 2.78 | 0 |
| 2026-09-21 | 3.23 | 3.23 | 0 |
| 2026-09-22 | 2.57 | 2.60 | +0.03 |
| 2026-09-23 | −0.37 | −0.37 | 0 |
| 2026-09-24 | 0.55 | 0.55 | 0 |

差异说明：09-21 样本 103 只、其中 1 只缺 D+1 行情；本服务按 102 只算均值，旧指标可能按全部 103 只或使用了不同缺失处理。**未修改旧指标**，只记录口径差异。

2. `first_board_track`（放宽到接近全量首板）与 `first_board` 分组：

| 日期 | first_board_track 候选 / 收盘均值 | 本服务样本 / 平均 |
| --- | --- | --- |
| 2026-09-17 | 17 / +1.04% | 38 / +2.52% |
| 2026-09-18 | 24 / +0.74% | 66 / +2.73% |
| 2026-09-21 | 35 / +0.50% | 82 / +2.68% |
| 2026-09-22 | 16 / −3.29% | 39 / −1.47% |
| 2026-09-23 | 20 / +0.01% | 39 / +0.29% |
| 2026-09-24 | 10 / −6.51% | 39 / −2.37% |

差异原因（口径不同，不强行对齐）：
- `first_board_track` 还要求「首封时间 ≤ 14:00」、流通市值/成交额/题材等条件，且剔除一字板等买不到样本；
- 本服务统计**全部** D 日首板（`consecutive ≤ 1`），不做可交易性剔除；
- 旧指标是「可买入样本的收盘收益」，本服务是「全部样本的日线涨跌幅」。

3. `limit_funnel`：断板集合直接复用 `build_promotion_funnel` 的逐券 `FAILED`，未复制分类逻辑。

---

## D. 股票研究侧栏（P1-B）

### D1. 页面接入清单

| 页面 / 组件 | 接入位置 | 说明 |
| --- | --- | --- |
| 涨停复盘 `LimitTable` | 名称旁「研」 | 传入当前 `date` 作为数据日 |
| 晋级漏斗 `PromotionFunnelStockTable` | 股票列 | 保留原 `StockLink` |
| 竞价异动 `AuctionAnomaly` | 名称旁 | 传入 `data.trade_date` |
| 板块成分 `Sectors.MemberTable` | 名称旁 | 行点击仍记录列表 |
| 形态选股 `Patterns` | 代码/名称链接后 | 原 `Link` 不动 |
| 自选股 `WatchlistPanel` | 名称旁（ETF 不显示） | ETF 没有个股页 |
| 选股池 `Pools` | 首板 / RPS / 板块个股三张表 | 原链接保留 |

### D2. URL 联动

- 打开：`?stock=600000`，保留 `date` / `foo` 等参数；
- 关闭：只删除 `stock`，其它参数不动；
- 非法代码（非 6 位数字）：不打开、不发请求、不改 URL；
- 刷新：从 `?stock=` 恢复；
- 浏览器前进/后退：`searchParams` 变化同步打开/关闭；
- 每次打开/关闭只推一条历史，无「点击一次两条导航」。

### D3. 多窗口联动

- 通道名 `zhifujing:stock-research`，消息只含 `{type, code, tab, scope}`；
- `scope` 是账户 ID + 用户名的不可逆哈希，**不广播 Cookie / Token / 账号明文 / 持仓**；
- 不同 `scope` 的消息直接忽略；
- 收到消息只更新本地状态与 URL，**不回广播**（测试断言 postMessage 次数不变）；
- 关闭开关后不建立通道；浏览器不支持 `BroadcastChannel` 时站内单窗口功能正常。

### D4. 登录 / 注销隔离

- 未登录：`openStockResearch` 直接返回，不打开、不发请求；
- 登出 / 换账户：清空选择、删除 URL `stock` 参数、清空模块缓存；
- 缓存键包含用户 ID，跨账户不共享。

### D5. StockLink / 前后翻兼容

- `StockLink` 的 `href` 仍为 `/stock/{code}`（测试断言）；
- 「研」按钮 `preventDefault + stopPropagation`，不触发行级 `rememberStockList` 或路由跳转；
- 原有列表页行点击、个股页 ← → 逻辑未改。

### D6. 各 Tab 加载与缓存统计（组件测试，网络为 mock）

| 操作 | 请求数 | 说明 |
| --- | --- | --- |
| 首次打开（概览） | 1 次 `stockProfile` | 标题与概览共用同一缓存键，在途去重 |
| 3 分钟内重开同一股票 | 0 次 | 命中缓存 |
| 切换「题材」 | +1 次 `stockThemes` | 只请求当前 Tab |
| 切换「涨停」 | +1 次 `stockLimitHistory` | |
| 切换「资金」 | +1 次 `stockDde(..., false)` | 只读库，不触发现取 |
| 切换「龙虎榜」 | +1 次 `stockLhb` | |
| 切换「K线」 | +1 次 `stockDaily` | 不调用 `syncStock` |
| 快速切换 10 只股票 | 10 次 `stockProfile` | 旧响应被序号守卫丢弃 |
| 数据日变化 | 重新请求 | 缓存键含数据日 |

- 缓存 TTL 3 分钟；401/403 与失败不缓存；最多 240 条；
- 不预取全市场、不新增外部数据源、不自动补历史。

### D7. 前端交互验收证据

本仓库**没有** Playwright/Cypress 等真实浏览器自动化环境（`frontend/package.json` 只有 Vitest + jsdom）。因此：

- 组件级交互测试覆盖：打开、关闭、快速切换、7 个页面入口、StockLink 回归、URL、广播、缓存、失败态、无 K 线、日期变化、注销隔离（28 个用例）；
- **没有**真实浏览器截图或端到端证据；不把组件测试冒充浏览器验收。

---

## E. 全量测试

### E1. 后端 pytest

```
.\.venv\Scripts\python.exe -m pytest -q
1367 passed, 3 skipped, 4 warnings in 316.88s
```

- 新增测试：`test_auction_s1.py`（9）、`test_sentiment_next_day.py`（12）、`test_stock_research_api.py`（3）；
- 跳过：3 个**既有**跳过用例，本批次未新增 skip；
- warning：4 条均为既有 SQLite 日期适配器 DeprecationWarning；
- 未删除任何失败用例；未写入真实数据库（测试全部使用临时 SQLite）。

### E2. Ruff

```
.\.venv\Scripts\python.exe -m ruff check backend tests scripts/simulate_auction_day.py
All checks passed!
```

顺带修掉两个**既有**测试文件的 lint 问题（未使用 import / 重复函数定义），不改变测试行为。

### E3. 前端

```
npm test        → 10 files / 108 tests passed
npm run lint    → 0 warnings, 0 errors（oxlint, 123 files）
npm run build   → tsc -b && vite build 成功
```

- 新增：`SentimentNextDay.test.tsx`（7）、`StockResearch.test.tsx`（15）、`StockResearchDrawer.test.tsx`（13）；
- 原有竞价异动组件 12 个用例、晋级漏斗 15 个、登录 4 个等全部通过。

### E4. 性能实测

**B（只读本地 b1_research.db，`group=first_board`，窗口截至 2026-09-24）**

| 窗口 | SQL 查询数 | 耗时 | 返回行 | 有效样本 |
| --- | --- | --- | --- | --- |
| 20 日 | 25 | 67.9 ms | 20 | 978 |
| 60 日 | 65 | 116.4 ms | 60 | 3346 |
| 120 日 | 125 | 255.0 ms | 120 | 7509 |
| 250 日 | 255 | 613.3 ms | 250 | 13975 |
| 断板 60 日 | 545 | 700.5 ms | 60 | 3143 |

- 查询数 ≈ 每个交易日 1 条日线 IN 查询 + 固定几条批量查询，**没有逐券 N+1**；
- 断板分组复用 P0-B 漏斗，每个交易日固定几次查询，60 日约 545 条、700 ms，可接受；
- 20 日窗口若越过本地数据末端，返回空样本/待观察，不报错。

**C（组件测试统计）**

- 首次打开 1 次请求；同股票重开 0 次；每切一个 Tab +1 次；
- 快速切换 10 只：10 次请求，旧响应不覆盖新股票；
- 缓存上限 240 条、TTL 3 分钟、在途去重；页面公共 `Layout` 块从 667 kB 降到 **8.8 kB**，抽屉单独 10.9 kB 按需加载。

### E5. 旧 API 兼容

- `/api/auction`、`/api/auction/boards`：仿真每个场景都调用并断言字段存在；
- `/api/auction/anomaly`：新增字段为可选，旧字段不变；
- `/api/stock/{code}/dde`：默认 `live=true`，老调用方行为不变；
- `/api/market/*`：只新增 `/api/sentiment/next-day`，原路由未改。

---

## F. 数据与授权边界

| 项目 | 结论 | 证据 |
| --- | --- | --- |
| C0 三因子未变 | 是 | `git diff` 未涉及 `factor_score.py`；blob `32b6f2ff...` |
| B1v 研究池未变 | 是 | 未修改 `scripts/forward_universe.py`（blob `2f8979d2...`） |
| C0 Top20 规则未变 | 是 | 未修改 `scripts/forward_track_c0.py`（blob `6ff3eb08...`） |
| 正式冻结快照 SHA256 未变 | 是 | `signals_2026-11-11.jsonl` = `1F80D1B6…F07B37`；`.meta.json` = `44301EB5…3FA2DD50` |
| 研究数据库未被本任务写入 | 是 | `forward_research.db` mtime 仍为 2026-11-11 17:17:58；本批次只读 |
| 生产数据库未被本任务写入 | 是 | `fupan.db` mtime 仍为 2026-10-31 22:26:44；所有查询 `mode=ro` |
| 未执行 Tushare | 是 | 无 Tushare 调用；C0 脚本未运行 |
| 未执行腾讯真实采集 | 是 | `fetch_spot` 仅在仿真中注入合成数据 |
| 未启动采集调度 | 是 | 未调用 scheduler / 未启动服务 |
| 未部署生产 | 是 | 未部署 `zhifujing.top` |
| 未合并 master | 是 | 只在 `feature/stock-screening-v2` 提交 |
| 未进行真实交易 | 是 | 无交易接口调用 |
| 无隐式 Schema 迁移 | 是 | 未新增表/列；启动仍走 `verify` 模式 |
| 新增只读 API 无写入 | 是 | `test_api_read_only`、`test_build_is_read_only`、`test_limit_history_is_read_only_and_desc` |

---

## 最终状态

通过：

- `S1_INTEGRATED_CODE_ACCEPTED`
- `P0_A_CODE_READY_FOR_LIVE_ACCEPTANCE`
- `SENTIMENT_NEXT_DAY_READY`
- `GLOBAL_STOCK_RESEARCH_READY`

继续保持：

- `REAL_AUCTION_DATA_NOT_YET_VERIFIED`
- `PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED`
- `P13_RELEASE_NO_GO`
- `HISTORICAL_DATA_GAP_UNDER_REVIEW`

停止条件：本批次完成，不自动启动真实行情采集、下一批开发或生产部署。
