# 致富经 B3：前瞻研究库交易日历延伸方案（未执行）

- 日期：2026-11-11
- 状态：`FORWARD_PAPER_TRACKING_STARTED`、`FIRST_FORWARD_FROZEN_SIGNAL_CREATED`
- 本文件只提交方案；**未写库、未调用任何外部 API**。

---

## 1. 现状（只读核对）

| 日历来源 | 覆盖范围 | 说明 |
| --- | --- | --- |
| `forward_research.db.trade_calendar` | 2026-09-01 ~ 2026-11-30（59 个交易日） | 初始化时按 `--calendar-start/--calendar-end` 写入 |
| `fupan.db.trade_calendar` | 1990-12-19 ~ 2026-12-31 | 本地已验证日历，只读源 |

2026 年 12 月的交易日（源库已包含）：
`12-01、12-02、12-03、12-04、12-07、12-08、12-09、12-10、12-11、12-14、
12-15、12-16、12-17、12-18、12-21、12-22、12-23、12-24、12-25、12-28、12-29、12-30、12-31`
共 23 天，当前独立库全部缺失。

## 2. 为什么需要延伸

首份信号（2026-11-11）按既有退出规则：

- 若 11-12 正常入场：计划退出 11-18，最晚退出尝试 **12-02**；
- 若入场延迟到 11-18：计划退出 11-24，最晚退出尝试 **12-08**。

因此**最晚需要覆盖 2026-12-08**；当前库止于 11-30，12-01 起的 6 个交易日缺失。

## 3. 可信来源

- 唯一来源：本地 `backend/data/fupan.db.trade_calendar`（只读、已验证，覆盖至 12-31）。
- 不访问网络、不调用 Tushare `trade_cal`、不使用自然日填充或工作日推算。
- 若源库缺任一需要的交易日 → 停止，不写库、不猜日期。

## 4. 拟执行命令（未执行）

```powershell
.venv\Scripts\python.exe scripts\forward_collect_tushare.py --init-calendar --db backend/data/forward_research.db --calendar-db backend/data/fupan.db --calendar-start 2026-11-01 --calendar-end 2026-12-31
```

说明：

- 只写 `backend/data/forward_research.db`；`fupan.db` 以 `mode=ro` 打开。
- 现有 `init_calendar()` 使用 `INSERT OR IGNORE` + 单事务，重复执行不会重复插入。
- 会校验：目标库名/路径、源日历存在、区间内有交易日、覆盖最近交易日、覆盖下一交易日。
- 建议延伸到 12-31（给后续信号留余量），而不是只补到 12-08。

## 5. 执行前后的核对清单

写入前（只读）：

1. 确认 `forward_research.db` 完整性 `ok`，且 `history.db`/`fupan.db` mtime 未变；
2. 记录目标日历当前条数（59）与最大日期（11-30）；
3. 确认源日历含 12-01 ~ 12-31 全部交易日；
4. 确认 12-08 与 12-09 均在源日历内（退出窗口 + 下一交易日）。

写入后：

1. 目标库日历条数 = 59 + 23 = 82；
2. 无重复日期（主键 + `COUNT(DISTINCT)=COUNT(*)`）；
3. 11-30 → 12-01 连续、12-08 存在、12-31 存在；
4. `PRAGMA integrity_check = ok`；
5. `stock_daily` / `universe_status` / `data_revision` / `ingest_log` 行数不变（只新增日历）；
6. 原库 mtime 不变；仓库无未预期文件变更。

## 6. 时间要求与边界

- 必须在**评价首个信号（或其延迟退出样本）进入 12 月之前**完成；
  建议在 2026-11-28 前完成，避免 12-02/12-08 到期时才发现缺日历。
- 不影响 11-11 已冻结快照；不重算信号、不改策略版本。
- 仍需用户单独授权（本轮不写库）；不启动自动定时、不合并 master、不部署。
