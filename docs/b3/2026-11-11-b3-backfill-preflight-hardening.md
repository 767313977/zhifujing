# 致富经 B3：40 日预热前阻断项整改（P0-1 ~ P0-5 + P1）

- 日期：2026-11-11
- 前置状态：`BACKFILL_PREFLIGHT_BLOCKED`
- 整改后状态：`READY_FOR_AUTHORIZED_BACKFILL`（仅采集器；未建正式库、未发请求）
- 保持：`FORWARD_TRACKING_NOT_STARTED`、`PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED`、
  `P13_RELEASE_NO_GO`、`HISTORICAL_DATA_GAP_UNDER_REVIEW`

---

## 1. P0-1 真实的 40 日预热日期

以本地 `trade_calendar`（只读 `backend/data/fupan.db`）为唯一来源，以 2026-11-11 为锚点
向前取 40 个交易日，写入 [forward_backfill_40d_plan.json](forward_backfill_40d_plan.json)。

| 项 | 值 |
| --- | --- |
| 起止 | 2026-09-09 ~ 2026-11-11 |
| 数量 | 40（严格升序、无重复） |
| 窗口前一日 | 2026-09-08 |
| 下一交易日 | 2026-11-12 |
| 日期哈希 | `sha256("\n".join(dates))` = `c63cd3c92b359467630b5bda9180f91ea58a5c8bb1e8ae388e4b32c9b9ff96a6` |
| 请求预算 | 40 次 `daily` |

执行时点：`--purpose backfill` 只接受当前日期之前的交易日，因此最后一日 2026-11-11
必须在 2026-11-12 或之后执行；不得当日回填。清单文件内已写入该约束。

## 2. P0-2 上市日期进入分市场基线

- `load_manifest()` 校验 `official_listing_date`：北交所 32 只必须提供官方日期，并与
  `docs/b3/bse_identity_2026-11-11.json` 逐只比对；缺失或冲突直接拒绝。
- `Manifest.split_by_listing(day)` 返回“已上市 / 明确未上市 / 上市日期未证实”：
  - 有官方日期：按日期判断；
  - 无官方日期的沪深证券：只有 `first_date ≤ D`（当日或之前已有过行情）才进入“已上市”，
    不推断上市日；更早的日子记 `listing_date_unverified`。
- `universe_status` 新增 `not_listed_yet` / `listing_date_unverified`；上市前缺席不再记
  `no_quote_unverified`，也不会被当成停牌。
- 北交所分市场分母随日期变化：09-09 = 27、09-16 = 28、09-24 = 31、09-30 = 32。
- 研究池仍是 B1v 冻结 5,108 只，未增加的证券、未改三因子。

## 3. P0-3 上一确认日与当日同口径

唯一口径：**冻结研究池内、且当日已上市**的证券。

- 当日分母：`manifest.expected_board_counts(day)`；
- 当日分子：`by_board_listed`（返回行 ∩ 已上市集合）；
- 上一确认日：`_previous_board_counts(conn, day, listed_codes)` 先按同一集合过滤，再计数；
- 清单外证券可以入库，但**不进入分子/分母**，不会再与全市场数量直接比较；
- `ingest_log.message` 记录 `expected_total / expected_by_board / by_board_listed /
  previous_by_board / listed / not_listed / listing_unverified / data_protocol /
  listing_protocol / manifest_version / eligibility_sha256`。

## 4. P0-4 修订保护

首版前瞻**禁用所有已确认历史日期修订**：

- CLI 删除 `--snapshot-dir`；`--revise` 保留但只会返回 `revise_disabled`；
- 不再依赖“同日快照是否存在”来判断，避免通过指定空目录绕过；
- 后续如需修订，必须另行设计可审计协议（记录冻结快照依赖的交易日集合与数据版本）。

## 5. P0-5 正式评价审计

`forward_track_c0` 新增：

- `detect_data_source()`：快照 meta 记录 `provider / db / data_protocol / units / pct_source`；
- `day_audit_problem(strict=True)`：正式评价要求当日 `data_revision`、成功 `ingest_log`
  与内容哈希同时成立；缺表、缺日志、版本不符、哈希不符都拒绝；
- `_forward_audit_problems()`：对 `FORWARD_FROZEN + provider=tushare` 的快照，审计信号日
  以及之后已存在的 10 个交易日内所有已采集日期（覆盖 D+1 入场与退出日）；未采集日期保持
  pending，不伪造收益；
- `revision_mismatch()`：快照版本与库内版本不一致、版本表/当日标记被删都拒绝；
- `REPLAY_ONLY` 不套用正式前瞻日志协议。

## 6. 数据协议版本

- `forward_store.DATA_PROTOCOL_VERSION = "forward-daily-v1"`：字段语义、单位换算、缺失状态；
- `forward_store.LISTING_PROTOCOL_VERSION = "bse-official-listing-v1"`：上市日期证据口径；
- 两者写入 `ingest_log` 与快照 meta；变更必须换版本号并重新验收，不静默改变 C0 股票池或因子。

## 7. 验证结果

- 采集器测试：**60 passed**（含上市日期四时点、上市前/后、133 只、北交所整段、事务注入、
  修订禁用、路径、日历、评价审计）。
- 40 日真实规模端到端（临时库、合成行情、5108 + 467 清单外）：
  - 40 日全部确认，40 个完成标记、40 条成功日志、0 条失败；
  - 每日 5,575 行（5,108 池内 + 467 清单外），北交所 27/28/31/32；
  - 重复执行 `already_confirmed` 且 0 次请求；
  - 恶意丢失北交所整段 → 拒绝，且未写完成标记；
  - `forward_problems` 返回空；冻结快照 provider=tushare、protocol=forward-daily-v1；
  - 评价正常返回 pending；改动任意一日行情后拒绝评价。
- 后端全量测试与静态检查见交付报告。

## 8. 仍需授权

1. 创建 `backend/data/forward_research.db` 并初始化日历；
2. 按本计划执行 40 次 `daily`（2026-11-12 起，每日一次，失败即停）；
3. 启动每日 live 采集与前瞻冻结；
4. 是否将分支同步到 master。
