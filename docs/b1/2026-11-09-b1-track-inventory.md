# B1.4 Track 路由与口径盘点

> 日期：2026-11-09
> 目的：接入统一回测内核前，先列清全部相关 track 路由、参数、响应、前端使用方与测试。
> 本轮**不替换**任何正式 track 计算，只做并行验证。

---

## 1. 路由清单

| 路由 | 服务 | 主要参数 | 响应类型 | 前端使用方 | 现有测试 |
| --- | --- | --- | --- | --- | --- |
| `GET /api/patterns/track` | `services.pattern_track.track` | `cohorts 1..120`、`top 1..200`、`track_days 1..120` | `PatternTrackOut` | `PatternTrack.tsx` | `tests/test_pattern_track.py` |
| `GET /api/patterns/track/detail` | `pattern_track.detail` | `date`、`top`、`track_days` | `PatternTrackDetail` | `PatternTrack.tsx` | 同上 |
| `GET /api/patterns/scorecard` | `pattern_track.scorecard` | `cohorts`、`track_days`、`line_days`、`dedup_days` | `PatternScorecardOut` | `PatternTrack.tsx` | 同上 |
| `GET /api/patterns/standing` | `pattern_track.standing` | `cohorts`、`track_days`、`line_days` | `PatternStandingOut` | `PatternTrack.tsx` | 同上 |
| `GET /api/pools/first-board/track` | `pools.first_board_track` | 首封、流通市值、成交额、题材数、封单比、真首板、`days`、`order` | `FirstBoardTrackOut` | `Pools.tsx` | `tests/test_pools_track.py` |
| `GET /api/pools/rps/track` | `pools.rps_track` | `min_rps`、`top`、`days`、`mode`、`breakout_only`、`trend_template`、`order` | `RpsTrackOut` | `Pools.tsx` | `tests/test_pools_track.py` |
| `GET /api/pools/sector-rps/track` | `sector_rps.track` | `top 1..30`、`days 5..60`、`taxonomy` | `SectorTrackOut` | `Pools.tsx` | `tests/test_sector_rps.py` |
| `GET /api/pools/sector-stock/track` | `pools.sector_stock_track` | `days 5..60`、`taxonomy`、`scope`、`order`、`sectors`、`per_sector` | `SectorStockTrackOut` | `Pools.tsx` | `tests/test_sector_stock.py` |
| `GET /api/pools/rps` | `pools.rps_board` | `mode`、`min_rps`、`trend_template`、`breakout_only`、`order`、`limit` | `RpsBoardOut` | `Pools.tsx` | `tests/test_pools_trend.py` |
| `GET /api/pools/sector-stock` | `pools.sector_stock` | `days`、`taxonomy`、`scope`、`order`、`sectors`、`per_sector` | `SectorStockOut` | `Pools.tsx` | `tests/test_sector_stock.py` |

---

## 2. 口径差异（旧 track vs 新内核）

| 项 | 旧 track | 新内核 | 是否可直接比较 |
| --- | --- | --- | --- |
| 入场 | 筛选日**收盘**（收盘→收盘） | 默认 T+1 **开盘**；`entry=close` 可对照 | 用 `entry=close` 可近似对照 |
| 基准 | 同一筛选日全市场等权（收盘口径） | 同一入场日/退出日全市场等权（默认次日开盘） | 收盘对照时基准时点不同，需标注 |
| 持有期 | 1..track_days（默认 30） | 任意 horizons（5/10/20/40/60） | 可对齐 |
| 不可成交 | 多数旧 track 未剔除开盘涨停；首板 track 有 | 统一剔除开盘涨停/退出跌停 | 旧口径偏乐观 |
| 费用 | 无 | 0/15 bps 可配 | 新口径更严 |
| 去重 | 形态 track 不去重；scorecard 可去重 | 默认不去重，可加 `dedup`（B1 未接） | 需注明 |
| 样本状态 | 多为「有行情就计入」 | `ok/unbuyable/suspended/no_exit/pending` 分状态 | 新口径更细 |

**结论**：只有 `patterns/track` 可以在 `entry=close` 下做严格同信号集对照；`first-board`、`sector-rps`、`sector-stock` 的收益口径不同（次日开盘/板块指数/1 日），只能做「方向一致性 + 差异原因」对照，不能直接要求数值相等。

---

## 3. 兼容性检查清单

| 检查 | 当前状态 |
| --- | --- |
| 旧接口默认参数不变 | 未修改任何路由签名 |
| 旧响应字段语义不变 | 未修改 Schema |
| 数据不足状态 | 旧接口已有 `progress`/`skipped`；新内核新增显式状态，不改旧字段 |
| 历史日期不引入未来数据 | 旧接口已有 `start`/`days` 约束；新内核 as-of 测试覆盖 |
| 鉴权 | 未新增对外路由；B1 只做脚本级并行验证 |
| 高成本查询上限 | 旧路由有 `cohorts/top/days/track_days` 上限；新内核 CLI 有 `max-calendar-days` |
| 不拖垮业务服务 | B1 验证在离线脚本执行，不挂到线上请求链路 |

---

## 4. B1 并行验证方式

- 新增 `scripts/compare_track_kernel.py`：
  - 用 `pattern_track._picks` 取与 `/patterns/track` **完全相同**的信号集；
  - 旧结果用 `pattern_track.track`；
  - 新结果用 `research_backtest.run_signals(entry_mode="close")`；
  - 逐 step 对比 mean/median/up_pct，输出差异与预期差异说明；
  - 不修改路由、Schema、前端。
- 其他 track 只输出口径映射，不做数值强制一致：
  - `first-board/track`：旧为次日开盘溢价 + 开盘→收盘；新内核 1 日 `next_open` 可对照方向；
  - `sector-rps/track`：旧是**板块指数**收益；新内核是个股收益，不可直接比；
  - `sector-stock/track`：旧只有 1 日；新内核可扩到 5/10/20，但样本/板块归属口径需另行对齐。

---

## 5. 本轮不做

- 不替换正式 track 计算；
- 不新增对外 API；
- 不改前端；
- 不切换生产流量；
- 不做组合层回测。
