# Trend Engine P0 闭环台账

> 更新时间：2026-11-15
> 范围：M1～M6（第一开发批次）。本批次**只读研究数据**，未安装 `data_revision`、未写生产库。
> 每项都必须有：根因 / 修复阶段 / 修改文件 / 测试用例 / 验收证据。

| 编号 | 问题 | 状态 | 修复阶段 |
| --- | --- | --- | --- |
| F1 | 研究库缺少 `data_revision`，缓存实际关闭 | **已设计，未安装**（按授权只做方案） | M1 / M7 |
| F2 | RPS 混版本与孤儿日 | **已修复（读取侧闸门）** | M1 |
| F3 | 距高点口径两套 | **已修复（统一函数）** | M1 |
| F4 | 板块 RPS 分位算法不一致 | **已修复** | M4 |
| F5 | 板块区间收益缺日静默跳过 | **已修复** | M4 |

---

## F1：`data_revision` 缺失导致研究缓存关闭

**根因**

- `research_screen._cached` 的缓存门禁依赖 `data_revision` 表 + `data_revision_meta` + 每张依赖表的 3 个触发器
  （`backend/app/services/data_revision.py:120-166`）；
- `b1_research.db` / `b2_final_snapshot.db` 实测**没有这些表** → `status()` 返回 `not_installed`
  → 每个请求都走 `compute()`，低启动这类全市场扫描每次 6 秒左右；
- 趋势页若沿用同一套缓存，会把「每日盘后一次」降级成「每次打开都全市场重算」。

**修复阶段**：M1 完成技术设计与安装方案；M7 前置条件。

**方案（本批次未执行）**

1. 在 `data_revision.DOMAINS` 增加：
   ```python
   "trend": ("trend_daily_scores", "trend_signal_snapshots", "trend_pool_members"),
   ```
2. 只在**隔离研究库**安装并验收：
   ```powershell
   python scripts\install_data_revision.py --db backend\data\b1_research.db --strict
   python scripts\install_data_revision.py --db backend\data\b1_research.db --status
   ```
3. 验收：8 个业务域 + 24 条触发器（新增 3 张表后为 33 条）；`--status` 不得为不存在的路径建空库；
   重复安装不得更换 `db_uuid`、不得清空 revision。
4. 回滚：保留安装前备份；删除触发器与 `data_revision*` 表即可回到 fail-closed 状态（不会丢业务数据）。

**修改文件**：`backend/app/services/data_revision.py`（仅设计，未改）
**测试**：`tests/test_data_revision.py`、`tests/test_install_data_revision.py`（既有）
**证据**：`reports/trend_engine/00-architecture-audit.md` §7.3、§10（B1）

---

## F2：RPS 混版本与孤儿日

**根因**

- `rps_daily` 历史上由不同版本的 `scan_rps` 写入：2026-10-21 那天 `ret20/ret60` 全空、
  `ret120/ret250` 有值（`docs/b0/2026-11-08-b0-data-audit.md` §2.4）；
- 同一天晚于个股日线末日（10-09），形成「孤儿 RPS」；
- 读取侧没有任何闸门，跨日研究会把不完整分位当成有效值。

**修复（读取侧闸门，不修改历史数据）**

- 新增 `backend/app/services/trend_engine/data_quality.py`
  - `rps_day_status(session, day)`：区分 `ok / no_bars / no_rps / no_rps60 / partial_rps / orphan_rps`；
  - `coverage_flags(...)`：RPS + 板块实际日期 + `sector_stale`；
  - 覆盖率阈值与 `services.rps.MIN_COVERAGE` 同为 0.9。
- 回测脚本在 `m6_data_quality.json` 里输出 `rps_end / index_end / sector_end / breadth_rows`，并注明「混版本」限制。

**修改文件**：`backend/app/services/trend_engine/data_quality.py`（新增）
**测试**：`tests/test_trend_data_quality.py`（5 项：no_bars / orphan / no_rps / no_rps60 / partial / ok / sector 滞后）
**证据**：测试通过；`reports/trend_engine/m6/m6_data_quality.json`

---

## F3：距高点口径两套

**根因**

- `research_screen._trend_rows` 用 `close[-1] / max(high[-60:]) - 1`（距最高**价**）；
- `pools._off_high_pct` 用 `pct_chg` 复利净值 / 最高净值（距最高**收盘**）；
- 两处都叫「距高点」，数值不同且没有版本记录。

**修复**

- `backend/app/services/trend_engine/indicators.py` 提供两个**命名明确**的纯函数：
  - `off_high_close_pct(close, window)`：距最高收盘；
  - `off_high_high_pct(high, close, window)`：距最高价；
  - `off_high_pct` 保留为 `off_high_close_pct` 的兼容别名。
- `research_screen` 改为调用 `off_high_high_pct`（数值与原来一致，但实现只有一份）；
- `pools._off_high_pct` 改为在净值曲线上调用 `off_high_close_pct`（语义不变、实现统一）。

**修改文件**：`indicators.py`、`research_screen.py`（`:486`）、`pools.py`（`:624-635`）
**测试**：`tests/test_trend_indicators.py::test_off_high_close_and_high_are_distinct_but_shared`、
`tests/test_research_screen.py`、`tests/test_pools*.py`
**证据**：73 项定向回归通过（2026-11-15）

---

## F4：板块 RPS 分位算法不一致

**根因**

- `sector_rps.rps20_on` 用 `rps.mid_rank`（并列同分）；
- `research_screen._sector_states` 用 `(index + 0.5) / n * 100`（并列按输入顺序拿不同分位）。

**修复**

- `research_screen` 新增 `_mid_rank_map`，与 `rps.mid_rank` 完全同源；
- `research_screen._sector_states` 的 `rps20_by_day` 全部改走 `_mid_rank_map`；
- `trend_engine/sector_strength.py` 同样复用 `research_screen._mid_rank_map` 与 `_compound_return`。

**修改文件**：`backend/app/services/research_screen.py`、`backend/app/services/trend_engine/sector_strength.py`
**测试**：`tests/test_trend_sector_market.py::test_sector_rps_uses_mid_rank_and_matches_sector_rps`、
`::test_sector_ties_get_same_percentile`
**证据**：与 `sector_rps.rps20_on` 同值（容差 0.1）、并列同分

---

## F5：板块区间收益缺日静默跳过

**根因**

- `_sector_states` 复利时 `if pct is not None: value *= ...`，缺日直接跳过；
- 只要窗口行数够 `window` 就算出「20 日涨幅」，实际可能只采到 12 天有效值。

**修复**

- 新增 `SECTOR_MIN_COVERAGE = 0.9` 与 `_compound_return(values, window)`：
  同时要求「至少 window 行」+「有效值占比 ≥90%」，否则返回 `None`（不进入横截面）；
- `sector_strength.py` 的 RET20/60 与 RPS 全部复用该函数。

**修改文件**：`research_screen.py`、`sector_strength.py`
**测试**：`tests/test_trend_sector_market.py::test_sector_coverage_gate_rejects_short_window`、
`::test_sector_coverage_gate_allows_one_missing_day`
**证据**：覆盖率 85% 的板块不进入排名（不是 0 分）；95% 正常计算
