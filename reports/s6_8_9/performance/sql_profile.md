# S6.8 F1：workbench SQL 剖析

- 数据库：`backend/data/b1_research.db`（只读 `mode=ro&immutable=1`）
- 日期 / 板块：`2026-09-24` / 5 个
- 总 SQL：**53**（预算 25）→ `PERF_SQL_BUDGET_NOT_MET`
- 唯一 fingerprint：40；总耗时 218.8 ms

| # | count | 分类 | 耗时 ms | fingerprint | 调用栈 | SQL 片段 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 4 | fixed | 1.67 | `cbbef80692371983` | data_quality.py:184:_stock_daily_items<br>data_quality.py:783:_safe_source<br>data_quality.py:820:_source_items | `SELECT max(stock_daily.trade_date) AS max_1 FROM stock_daily` |
| 2 | 3 | per_sector | 10.508 | `49959a85cee6afeb` | post_market_workbench.py:198:_resonance_block<br>post_market_workbench.py:259:build<br>post_market_workbench.py:261:build | `SELECT trade_calendar.trade_date FROM trade_calendar ORDER BY trade_calendar.trade_date` |
| 3 | 3 | fixed | 1.913 | `8946a84e809170e6` | limit_funnel.py:469:_pool_rows<br>limit_funnel.py:481:load_funnel_inputs<br>limit_funnel.py:482:load_funnel_inputs | `SELECT limit_pool.trade_date, limit_pool.code, limit_pool.pool_type, limit_pool.name, limit_pool.pct_chg, limit_pool.pri` |
| 4 | 3 | per_sector | 1.327 | `6b787c596736d789` | post_market_workbench.py:198:_resonance_block<br>sector_resonance.py:112:_latest_log<br>sector_resonance.py:123:_pool_trust | `SELECT collect_log.id, collect_log.trade_date, collect_log.task, collect_log.status, collect_log.rows, collect_log.messa` |
| 5 | 3 | per_sector | 0.67 | `137c2fe097b93ca0` | post_market_workbench.py:198:_resonance_block<br>post_market_workbench.py:263:build<br>sector_resonance.py:124:_pool_trust | `SELECT count(*) AS count_1 FROM limit_pool WHERE limit_pool.trade_date = ? AND limit_pool.pool_type = ?` |
| 6 | 2 | fixed | 0.978 | `34ec7260f08f97e4` | data_quality.py:308:_latest_ok_day<br>data_quality.py:371:_limit_pool_items<br>data_quality.py:783:_safe_source | `SELECT collect_log.trade_date FROM collect_log WHERE collect_log.task = ? AND collect_log.trade_date IS NOT NULL AND col` |
| 7 | 2 | fixed | 0.703 | `d1880b2d513769f7` | data_quality.py:537:_market_sentiment_items<br>data_quality.py:783:_safe_source<br>data_quality.py:820:_source_items | `SELECT market_sentiment.trade_date, market_sentiment.limit_up_count, market_sentiment.limit_down_count, market_sentiment` |
| 8 | 1 | per_sector | 22.679 | `32bf396808d22793` | data_quality.py:477:_sector_member_items<br>data_quality.py:783:_safe_source<br>data_quality.py:820:_source_items | `SELECT sector_member.trade_date, sector_basic.taxonomy, count(*) AS count_1, count(distinct(sector_member.sector_code)) ` |
| 9 | 1 | per_sector | 3.071 | `dcfba6623519c4c6` | post_market_workbench.py:198:_resonance_block<br>post_market_workbench.py:263:build<br>sector_resonance.py:401:build | `SELECT limit_pool.trade_date, limit_pool.code, limit_pool.pool_type, limit_pool.name, limit_pool.pct_chg, limit_pool.pri` |
| 10 | 1 | per_sector | 2.466 | `6f5d72d73abf2d14` | post_market_workbench.py:198:_resonance_block<br>post_market_workbench.py:263:build<br>sector_resonance.py:142:_quotes | `SELECT stock_daily.code, stock_daily.pct_chg, stock_daily.open, stock_daily.close FROM stock_daily WHERE stock_daily.tra` |
| 11 | 1 | fixed | 2.186 | `30d3f667b1c419e9` | auction_anomaly.py:205:frame_logs_by_day<br>data_quality.py:599:_auction_items<br>data_quality.py:783:_safe_source | `SELECT collect_log.id, collect_log.trade_date, collect_log.task, collect_log.status, collect_log.rows, collect_log.messa` |
| 12 | 1 | fixed | 1.954 | `2caaabacad855213` | data_quality.py:185:_stock_daily_items<br>data_quality.py:783:_safe_source<br>data_quality.py:820:_source_items | `SELECT stock_daily.trade_date, count(*) AS count_1, count(stock_daily.close) AS count_2, count(stock_daily.pct_chg) AS c` |
| 13 | 1 | fixed | 1.473 | `65f1890711ed92b3` | data_quality.py:105:_trade_days<br>data_quality.py:833:overview<br>post_market_workbench.py:138:_quality_block | `SELECT trade_calendar.trade_date FROM trade_calendar WHERE trade_calendar.trade_date <= ? ORDER BY trade_calendar.trade_` |
| 14 | 1 | per_sector | 1.331 | `c950bdd9dce2c5f0` | post_market_workbench.py:198:_resonance_block<br>post_market_workbench.py:263:build<br>sector_resonance.py:536:build | `SELECT market_sentiment.trade_date AS market_sentiment_trade_date, market_sentiment.limit_up_count AS market_sentiment_l` |
| 15 | 1 | fixed | 1.269 | `d5330b00a2ff82d4` | data_quality.py:308:_latest_ok_day<br>data_quality.py:371:_limit_pool_items<br>data_quality.py:783:_safe_source | `SELECT collect_log.trade_date FROM collect_log WHERE collect_log.task = ? AND collect_log.trade_date IS NOT NULL AND col` |
| 16 | 1 | fixed | 1.251 | `25ed8c0d1c684670` | post_market_workbench.py:105:_promotion_block<br>post_market_workbench.py:262:build<br>trading_days.py:112:recent_trade_days | `SELECT trade_calendar.trade_date FROM trade_calendar WHERE trade_calendar.trade_date < ? ORDER BY trade_calendar.trade_d` |
| 17 | 1 | fixed | 0.811 | `c1ede1a3c5297ee5` | post_market_workbench.py:261:build<br>post_market_workbench.py:67:_sentiment_block<br>sentiment_next_day.py:454:build_next_day | `SELECT limit_pool.trade_date, limit_pool.code, limit_pool.pool_type, limit_pool.consecutive FROM limit_pool WHERE limit_` |
| 18 | 1 | per_sector | 0.787 | `cc9da87779e0f158` | post_market_workbench.py:198:_resonance_block<br>post_market_workbench.py:263:build<br>sector_resonance.py:213:build | `SELECT sector_basic.code, sector_basic.name, sector_basic.taxonomy, sector_basic.updated_at FROM sector_basic WHERE sect` |
| 19 | 1 | fixed | 0.744 | `bfd1c11b92cf2e8e` | limit_funnel.py:547:load_funnel_inputs<br>post_market_workbench.py:113:_promotion_block<br>post_market_workbench.py:262:build | `SELECT stock_concept.code, stock_concept.concept FROM stock_concept WHERE stock_concept.trade_date = ? AND stock_concept` |
| 20 | 1 | per_sector | 0.597 | `d1a61f7efc67e332` | post_market_workbench.py:198:_resonance_block<br>post_market_workbench.py:263:build<br>sector_resonance.py:313:build | `SELECT sector_member.code, sector_member.name, sector_member.sector_code, sector_member.fetched_at FROM sector_member WH` |

## 结论

- 本轮只做剖析，**未改查询、未改缓存、未调预算**；
- `PERF_SQL_BUDGET_NOT_MET` 原样保留；
- 优化前置条件：结果语义、PIT、事务一致性和跨用户缓存隔离不变，并有等价性测试与前后对照。
