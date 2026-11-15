# M6.1 数据质量审计

- 数据库：`C:\Users\76731\Documents\trae_projects\fupan\backend\data\history.db`
- SHA256：`9267652175594871B49456253BB0437CD5738F0D19257AB8CCA961BA5F921A6A`
- 生成时间：2026-11-15
- 访问方式：`mode=ro` + `PRAGMA query_only=ON`

## 1. 表级覆盖

| 表 | 行数 | 起 | 止 | 交易日 | 中位行/日 | 重复键组 | 缺失交易日 | 覆盖(相对个股) | 滞后 | PIT |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| stock_daily | 5820960 | 2021-09-14 | 2026-10-19 | 1230 | 4869 | 0 | 0 | 1.0 | 0 | CLOSE_ONLY |
| index_daily | 0 | None | None | 0 | None | None | None | None | None | NO_DATA |
| sector_daily | 0 | None | None | 0 | None | None | None | None | None | NO_DATA |
| sector_member | 0 | None | None | 0 | None | None | None | None | None | NO_DATA |
| rps_daily | 0 | None | None | 0 | None | None | None | None | None | NO_DATA |
| breadth_daily | 0 | None | None | 0 | None | None | None | None | None | NO_DATA |
| market_sentiment | 0 | None | None | 0 | None | None | None | None | None | NO_DATA |
| fear_greed_daily | 0 | None | None | 0 | None | None | None | None | None | NO_DATA |
| limit_pool | 0 | None | None | 0 | None | None | None | None | None | NO_DATA |
| trade_calendar | 0 | None | None | 0 | None | None | None | None | None | NO_DATA |
| stock_basic | 0 | None | None | 0 | None | None | None | None | None | NO_DATA |
| pattern_hit | 0 | None | None | 0 | None | None | None | None | None | NO_DATA |

## 2. 个股日线

- 股票数：5108；≥250 根：4991
- 最早：2021-09-14；最晚：2026-10-19；交易日：1230
- 逐日行数：中位 4869 / 最大 5094
- 逐日 name 非空率：1.0

## 3. 指数与板块对齐

- index_daily：None ~ None，比个股末日滞后 None 个交易日；表不存在
- sector_daily：None ~ None，比个股末日滞后 None 个交易日；表不存在
- sector_member：None ~ None，比个股末日滞后 None 个交易日；表不存在

## 4. RPS

- 无 rps_daily 表

## 5. 市场宽度重算（不写库）

- 对照结果：{'status': 'NO_TABLE'}
- 最近 5 个交易日建议值：
  - 2026-10-13 上涨 2694 / 下跌 2133 / 平盘 137，站上 MA20 33.58%，站上 MA60 43.56%
  - 2026-10-14 上涨 867 / 下跌 4024 / 平盘 75，站上 MA20 23.71%，站上 MA60 35.72%
  - 2026-10-15 上涨 387 / 下跌 4554 / 平盘 25，站上 MA20 15.02%，站上 MA60 22.83%
  - 2026-10-16 上涨 2888 / 下跌 1966 / 平盘 113，站上 MA20 16.95%，站上 MA60 25.5%
  - 2026-10-19 上涨 2104 / 下跌 2726 / 平盘 134，站上 MA20 17.86%，站上 MA60 22.81%

## 6. ST 与退市

- ST：{'status': 'OK', 'day_count': 1230, 'first': ('2021-09-14', 1, 134), 'last': ('2026-10-19', 122, 4964), 'codes_with_st_transition': 0, 'pit_note': '使用 stock_daily.name 的当日值；同一天更名为 ST 的时点无法证明在收盘前已知'}
- 退市近似：{'status': 'OK', 'codes': 5108, 'stale_codes_30_trade_days': 0, 'sample': [], 'pit_note': '退市股无法从现有库确认；stale 只是「最后一行显著更早」的近似，不能等同于退市'}

## 7. 结论

- 未发现 30 个交易日以上的 stale 代码：若为 history.db，则确认无退市股（幸存者偏差）
- stock_basic.industry / total_mv 是当前快照，不能用于历史行业/市值分组（前视）
