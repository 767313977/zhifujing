# 数据缺口报告

- 数据库：`C:\Users\76731\Documents\trae_projects\fupan\backend\data\history.db`
- SHA256：`9267652175594871B49456253BB0437CD5738F0D19257AB8CCA961BA5F921A6A`

| 表 | 缺失交易日 | 示例 | 滞后(交易日) | 覆盖 |
| --- | ---: | --- | ---: | ---: |
| stock_daily | 0 | — | 0 | 1.0 |
| index_daily | None | — | None | None |
| sector_daily | None | — | None | None |
| sector_member | None | — | None | None |
| rps_daily | None | — | None | None |
| breadth_daily | None | — | None | None |
| market_sentiment | None | — | None | None |
| fear_greed_daily | None | — | None | None |
| limit_pool | None | — | None | None |
| trade_calendar | None | — | None | None |
| stock_basic | None | — | None | None |
| pattern_hit | None | — | None | None |

## 不可用字段

- `sector_daily.up_count/down_count`：开盘红未提供，恒为 NULL，板块广度改用 `sector_member`。
- `breadth_daily`：本库为空，环境判定只能降级为指数口径。
- 退市名单 / 历史 ST 状态表：不存在；只能用当日 `stock_daily.name` 与 stale 近似。