# 数据缺口报告

- 数据库：`C:\Users\76731\Documents\trae_projects\fupan\backend\data\b1_research.db`
- SHA256：`A93BC080EB9E366D08025C0A247E9AF525674A0294EC4D6012B7DBFD8941C56B`

| 表 | 缺失交易日 | 示例 | 滞后(交易日) | 覆盖 |
| --- | ---: | --- | ---: | ---: |
| stock_daily | 0 | — | 0 | 1.0 |
| index_daily | 0 | — | 5 | 0.0012 |
| sector_daily | 0 | — | 5 | 0.073 |
| sector_member | 0 | — | 5 | 11.7072 |
| rps_daily | 0 | — | 0 | 0.9962 |
| breadth_daily | 0 | — | None | None |
| market_sentiment | 0 | — | 5 | 0.0002 |
| fear_greed_daily | 0 | — | 5 | 0.0002 |
| limit_pool | 0 | — | 5 | 0.0217 |
| trade_calendar | 0 | — | 0 | 0.0002 |
| stock_basic | None | — | None | None |
| pattern_hit | 74 | 2026-05-15, 2026-05-18, 2026-05-19, 2026-05-20, 2026-05-21, 2026-05-22, 2026-05-25, 2026-05-26, 2026-05-27, 2026-05-28, 2026-05-29, 2026-06-01, 2026-06-02, 2026-06-03, 2026-06-04, 2026-06-05, 2026-06-08, 2026-06-09, 2026-06-10, 2026-06-11 | 0 | 0.1511 |

## 不可用字段

- `sector_daily.up_count/down_count`：开盘红未提供，恒为 NULL，板块广度改用 `sector_member`。
- `breadth_daily`：本库为空，环境判定只能降级为指数口径。
- 退市名单 / 历史 ST 状态表：不存在；只能用当日 `stock_daily.name` 与 stale 近似。