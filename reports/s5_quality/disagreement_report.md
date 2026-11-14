# S5-B 质量契约对账报告

- 生成日期：2026-11-14（Asia/Shanghai）
- 质量协议：`data-quality-v2`
- 对账器：`scripts/check_s5_quality_consistency.py`（只读、不外呼）
- 机器结果：`reports/s5_quality/contract_matrix.json`
- 数据库：`backend/data/b1_research.db`（`mode=ro&immutable=1`）
- 抽样区间：2026-09-01 ~ 2026-09-21，共 15 个交易日
- Git SHA：见 `contract_matrix.json` 的 `git_sha`

## 1. 执行的命令

```powershell
.venv/Scripts/python.exe scripts/check_s5_quality_consistency.py `
  --db backend/data/b1_research.db --start 2026-09-01 --end 2026-09-30 `
  --sample 15 --out reports/s5_quality/contract_matrix.json
```

结果：`days=15 checks=60 unexpected=0 sql=1216 elapsed_ms=4687.38`（本机只读库冷启动单次实测；同一脚本前一次运行为 2646 ms，差异来自磁盘/页缓存）。

## 2. 对账矩阵

| # | 对照 | 判据 | 15 日结果 |
| --- | --- | --- | --- |
| 1 | `/api/quality/overview` ↔ `/api/auction/anomaly` | 竞价双维度状态与 `assess_day_quality` 映射一致 | 15/15 通过（全部 `NOT_COLLECTED`：真实库竞价仍 0 行） |
| 2 | `/api/quality/overview` ↔ `/api/limit/promotion-funnel` | up 池来源状态与 `today_up_quality` 一致 | 15/15 通过（`VERIFIED_SOURCE` ↔ `ok`） |
| 3 | `/api/quality/timeline(sector_member)` ↔ `/api/sectors/theme-search` | 两边都不得宣称完成证明 | 15/15 通过（均为 `completion_proof=false`） |
| 4 | `/api/quality/overview` ↔ `/api/sentiment/next-day` | 次日自称 VERIFIED 时，D+1 日线不得「未采集」，且抽样缺失数必须为 0 | 15/15 通过（`daily=PARTIAL` 仅因全市场个别行缺字段；`missing=0`） |

汇总：`total_checks=60, passed=60, explained_disagreements=0, unexpected_disagreements=0`。

## 3. 发现并修复的一次口径冲突

首轮把判据写成「情绪次日 `VERIFIED` ⇒ 全市场 `stock_daily == VERIFIED`」，结果 6 个日期报矛盾：

```text
next_day=2026-09-02; daily=PARTIAL; sentiment=VERIFIED
```

核查后确认不是数据错误：

- `data_quality._stock_daily_items` 统计**全市场**每一行，任一 `close` / `pct_chg` 为 NULL 即 `PARTIAL`；
- `sentiment_next_day` 只抽样其分组（昨日涨停/炸板/断板）股票，`missing_count=0` 表示**抽样内**无缺失。

处理方式：**加强而不是放宽**判据——要求 `daily ∈ {VERIFIED, PARTIAL}` **且** `missing_count == 0`。这样：

- 全市场个别行缺字段不再误报；
- 若抽样内真有缺失却自称 `VERIFIED`，仍然判矛盾；
- 若 D+1 日线整体 `NOT_COLLECTED`，仍然判矛盾。

复跑后 `unexpected=0`。未删除任何对账用例。

## 4. 已知且允许的差异（代码内显式标注）

| 场景 | 质量台 | 业务服务 | 处理 |
| --- | --- | --- | --- |
| 最新一次 `pool_up` 失败，但历史 `ok` 快照仍在 | `PARTIAL`（`LATEST_ATTEMPT_FAILED_SNAPSHOT_RETAINED`） | 漏斗按最新日志严格判 `failed` | 对账器标 `explained=true`，不判矛盾 |
| 竞价 0926 有效、0931 缺失 | `PARTIAL` | 竞价异动日期级 `INCOMPLETE` | 对账器标 `explained=true` |

本轮 15 个真实日期没有触发上述差异（`explained=0`），但代码路径由 `tests/test_s5_quality_contract.py` 覆盖。

## 5. 合成边界覆盖（单元测试，不是真实行情）

`tests/test_s5_quality_contract.py` 共 30 个用例，覆盖真实库当前不存在的场景：

- 0926 有效 + 0931 缺失 → `PARTIAL`、`completion_proof=false`；
- 0926 `partial` → 终值 `INCOMPLETE`；
- 全部帧 `failed` → `UNVERIFIABLE`；
- 有行但无关键帧日志 → 不 `VERIFIED`；
- 9:31 前 → `post_open_status=PENDING`；
- up 成功 + broken 失败/缺日志 → 组合视图不 `VERIFIED`；
- 三池全 `ok` → 组合视图 `VERIFIED`；
- 最新失败 + 旧快照 → `previous_snapshot_available=true`、`current_attempt_failed=true`；
- `OperationalError` → `SOURCE_ERROR` + `DB_SCHEMA_UNAVAILABLE` / `DB_QUERY_FAILED`，不伪装 `NOT_COLLECTED`，不泄露 SQL/路径；
- 单来源异常不影响其他来源；timeline 保留错误桶；
- 全程只读。

## 6. 明确未完成的验收

- 真实竞价库仍为 **0 行**：`REAL_AUCTION_DATA_NOT_YET_VERIFIED` 继续保留；
- `sector_member` 仍无成员完成证明：`THEME_REAL_DATA_ACCEPTANCE_PENDING` 继续保留；
- 本报告全部为本地只读库 + 临时库单元测试，未发起任何外部请求。
