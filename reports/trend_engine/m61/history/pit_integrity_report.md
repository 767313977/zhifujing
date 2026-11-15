# PIT 完整性报告

- 数据库：`C:\Users\76731\Documents\trae_projects\fupan\backend\data\history.db`

| 表 | PIT 状态 | 来源 | 允许用途 |
| --- | --- | --- | --- |
| stock_daily | CLOSE_ONLY | iFinD / 腾讯（按采集路径） | T 日收盘后因子与次日开盘成交；不得假设盘中已知 |
| index_daily | NO_DATA | UNKNOWN | 市场环境（收盘后） |
| sector_daily | NO_DATA | UNKNOWN | 板块强度（收盘后）；滞后日期必须 fallback |
| sector_member | NO_DATA | UNKNOWN | 仅按当日快照使用，禁止用最新成分回看历史 |
| rps_daily | NO_DATA | UNKNOWN | 横截面相对强度；混版本日拒绝 |
| breadth_daily | NO_DATA | UNKNOWN | 市场宽度；为空时不得伪造 |
| market_sentiment | NO_DATA | UNKNOWN | — |
| fear_greed_daily | NO_DATA | UNKNOWN | — |
| limit_pool | NO_DATA | UNKNOWN | — |
| trade_calendar | NO_DATA | UNKNOWN | 仅作日期轴；未来日期不能当数据边界 |
| stock_basic | NO_DATA | UNKNOWN | 仅当前展示；历史分组禁用 industry/total_mv |
| pattern_hit | NO_DATA | UNKNOWN | 只覆盖扫描日；不能当连续信号源 |

## 已知 PIT 缺口

- 无 `created_at` / 首次入库时间：`stock_daily`、`index_daily` 只能证明日终事实。
- 历史行业与市值不可得：`stock_basic` 是当前快照。
- 历史 ST 以当日名称近似；更名当天的盘中可知性无法证明。
- 退市股无法识别：`history.db` 按当前代码池回补，0 只退市股。