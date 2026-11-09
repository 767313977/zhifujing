# B0.3 RPS 多周期扩展与迁移说明

> 日期：2026-11-08
> 模型：`backend/app/models.py` → `RpsDaily`
> 计算：`backend/app/jobs/scan_rps.py`
> 迁移：`scripts/migrate_rps_columns.py`
> 回补：`scripts/backfill_rps.py`
> 测试：`tests/test_scan_rps.py`、`tests/test_migrate_rps_columns.py`

---

## 1. 字段扩展

`rps_daily` 新增 4 个**可空**列：

| 列 | 含义 |
| --- | --- |
| `ret20` | 20 个交易日复利涨幅（%） |
| `ret60` | 60 个交易日复利涨幅（%） |
| `rps20` | `ret20` 在当日横截面的中位秩百分位（0~100） |
| `rps60` | `ret60` 在当日横截面的中位秩百分位（0~100） |

旧列 `ret50/120/250`、`rps50/120/250` 的名称、含义、主键、写入逻辑**不变**。

`scan_rps.WINDOWS` 从 `(50, 120, 250)` 扩为 `(20, 50, 60, 120, 250)`。窗口起点仍按交易日历取第 N 个最近交易日；`max(WINDOWS)` 仍是 250，所以旧窗口的取数范围没有变化。

---

## 2. 迁移与回滚

### 2.1 迁移

```bash
python scripts/migrate_rps_columns.py --db backend/data/b0_trial.db --status
python scripts/migrate_rps_columns.py --db backend/data/b0_trial.db --apply \
    --backup backend/data/b0_trial_before_rps.db
```

- 只执行 `ALTER TABLE rps_daily ADD COLUMN ... FLOAT`；
- 不建新表、不改旧列、不重建索引；
- 重复执行是 no-op（已存在的列跳过）；
- 默认拒绝写 `backend/data/fupan.db`，除非显式 `--allow-main-db`；
- 执行前可用 `--backup` 走 SQLite 在线备份 API。

实测迁移前后：

```
迁移前：trade_date, code, ret120, ret250, rps120, rps250, ret50, rps50
迁移后：trade_date, code, ret120, ret250, rps120, rps250, ret50, rps50, ret20, ret60, rps20, rps60
```

### 2.2 回滚

```bash
python scripts/migrate_rps_columns.py --db backend/data/b0_trial.db --rollback \
    --backup backend/data/b0_trial_before_rollback.db
```

- SQLite 3.35+ 直接 `DROP COLUMN`；
- 若部分列缺失，走「重建表 → 只保留旧 7 列 → 改名」兜底；
- 实测回滚后旧列与旧数据逐行保留（`ret50=1.0`、`rps250=30.0` 的样本行不变）；
- 单元测试 `tests/test_migrate_rps_columns.py` 覆盖 apply 幂等 + rollback 保留旧数据。

---

## 3. 计算口径与分母

### 3.1 与旧三档完全同一份

复用 `services/rps.py`：

- 区间涨幅用 `pct_chg` 复利；
- 窗口按交易日截取；
- 窗口内有效行数 ≥90%，否则该档 `None`；
- 横截面分位用中位秩；
- 当天没有行情的票不参与当天横截面。

### 3.2 分母不是同一批票

每个窗口的分母 = **当天该窗口有值的票**。因为窗口越长、覆盖率门槛导致的缺失越多，分母会不同。实测 2026-10-09：

| 窗口 | 分母（有值票数） |
| --- | ---: |
| 20 日 | 5,211 |
| 50 日 | 5,058 |
| 60 日 | 5,053 |
| 120 日 | 5,045 |
| 250 日 | 4,997 |

**解释边界**：

- 不能把 `rps20=90` 与 `rps250=90` 理解成「同一批股票里的同一个相对位置」；
- 分母包含 ST / 北交所（RPS 是全市场横截面）；研究池若剔除 ST/BSE，只影响候选，不影响 RPS 分母；
- 没有真实历史成分股，历史覆盖率分母用「目标日之前 20 个交易日行数中位数」估计（见 §4）。

### 3.3 2026-10-21 的混版本证据

| 日期 | 行数 | `ret50` | `rps50` | `ret120` | `rps120` | `ret250` | `rps250` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-10-09 | 5,115 | 5,058 | 5,058 | 5,088 | 5,088 | 5,038 | 5,038 |
| 2026-10-21 | 5,091 | **0** | **0** | 5,091 | 5,091 | 5,036 | 5,036 |

10-21 是 50 日窗口上线前写入的旧版本数据。B0.3 的回补脚本用**当前版本**重算目标区间，避免跨版本混用。

---

## 4. 历史回补与覆盖率

### 4.1 覆盖率闸

`scan_rps.scan()` 新增：

- `pool_count`：历史回补传入「当时股票池」估计值；
- `with_meta=True`：返回 `pool / pool_basis / denominators / day_rows`。

`estimate_pool_count(session, target)` = `target` 之前 20 个交易日的「当天有 `pct_chg` 的行数」中位数。

- 能识别「某一天只采了几百只」的残缺日；
- **识别不了**「整段窗口都残缺」；
- 不是真实历史成分股，报告里必须标注 `estimated_history`。

默认 `pool_count=None` 时仍用当前 `stock_universe`，与加参数前完全一致。

### 4.2 回补脚本

```bash
python scripts/backfill_rps.py --db backend/data/b0_trial.db --days 3 --report rps.json
python scripts/backfill_rps.py --db backend/data/b0_trial.db \
    --start 2025-09-25 --end 2025-11-05 --max-days 40
```

- 必须显式 `--db`；默认拒绝主库；
- 只处理「交易日历 ∩ 当天有行情」的日期；
- 逐日 upsert，可重复执行；
- `--max-days` 硬上限，超过报错，不静默截断；
- 逐日输出 `pool / pool_basis / denominators`。

### 4.3 幂等与旧值一致性

- 同一日期重复回补：行数不增、数值不变（单测 `test_scan_is_idempotent`）。
- 2026-10-09 新旧值对照（主库 vs 回补后的隔离库）：
  - 共同代码 5,115 只；
  - `ret50/ret120/ret250/rps50/rps120/rps250` 六列**0 个字段差异**（容差 0.011）；
  - 回补库多 155 只：覆盖率口径更全（旧扫描可能因当前股票池分母而少写），属于新增覆盖，不是旧值被改写。

---

## 5. 新旧兼容

| 调用方 | 兼容性 |
| --- | --- |
| `scan()` 默认返回 | 仍是 `{rows, trade_date, cost_seconds}`（单测断言） |
| `with_meta=False` | 不返回新键 |
| `pools.rps_board` | 只显式 select `ret50/120/250`，不受新列影响 |
| `rps_track` / `sector_rps` | 不依赖新列 |
| 现有 RPS 选股业务 | 未改阈值、未改排序、未改接口参数 |

---

## 6. 250 日回补结果

| 项 | 结果 |
| --- | --- |
| 区间 | 2025-09-22 ~ 2026-10-09（250 个交易日） |
| 写入 | 1,269,453 行 |
| 跳过 | 0 天 |
| 耗时 | 约 31 分 19 秒 |
| 每日行数 | 约 4,994 ~ 5,227 |
| 分母范围（首日 → 末日） | 20 日 4,981→5,211；50 日 4,970→5,058；60 日 4,962→5,053；120 日 4,966→5,045；250 日 4,917→4,997 |
| 旧值一致性 | 2026-10-09 共同代码 5,115 只，`ret50/120/250/rps50/120/250` 0 字段差异 |

回补脚本逐日输出见 `backend/data/b0_rps_backfill_250.json`（未提交仓库）。

---

## 7. 已知边界

1. 历史 RPS 百分位**不是**真实历史成分股横截面；分母是「当天有行情的票」，缺退市股。
2. 早期日期若不足 250 个交易日，`ret250/rps250` 为 `None`；不得用短窗口冒充长窗口。
3. ST/BSE 进入 RPS 分母；研究池需在候选层过滤。
4. `rps_daily` 只存每天一行，不保存分母与版本；审计时需用 `backfill_rps.py` 的逐日输出。
5. 回补是 CPU/IO 操作，不耗 iFinD 配额；但 250 天约 30 分钟，需在隔离库执行。
