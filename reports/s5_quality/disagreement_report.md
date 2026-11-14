# S5 数据质量对账（只读、抽样）

- 生成时间：2026-11-14T23:20:35
- Git：`c82fecef071df5915bc6129f23aaaf70ba7c9ba7`（分支 `fix/s5-third-review`，工作区 dirty=False）
- 命令：`scripts/check_s5_quality_consistency.py --db backend/data/b1_research.db --start 2026-09-01 --end 2026-09-30 --sample 15 --out reports/s5_quality/contract_matrix.json --md reports/s5_quality/disagreement_report.md`
- 环境：Python 3.13.13 / Node v24.19.0 / SQLite 3.50.4
- 退出码：2
- 数据库：`b1_research.db`（ro&immutable=1）
- 区间：2026-09-01 ~ 2026-09-30
- 矩阵版本：`quality-consistency-matrix-v3`

## 抽样口径

- 策略：等距抽样：--sample >= 区间交易日数时全量；否则按 (n-1)/(sample-1) 均匀取索引，样本唯一递增并覆盖区间首尾
- 请求样本：15；区间交易日：21；实际抽样：15
- 已抽日期：2026-09-01、2026-09-02、2026-09-04、2026-09-07、2026-09-09、2026-09-10、2026-09-14、2026-09-15、2026-09-16、2026-09-18、2026-09-21、2026-09-23、2026-09-24、2026-09-29、2026-09-30
- 覆盖率：0.7143；是否全历史：False
- 说明：抽样对账不等于全历史完整验收；未覆盖日期见 uncovered_days

## 结果

- 检查总数：76
- 通过：65
- 已解释差异：4
- 不可比 / 依赖未解决：7
- 未解释差异：0
- SQL：1239；耗时：2689.98 ms

## 不可比 / 依赖未解决（不计 PASS）

- 2026-09-14 · sentiment.d1_coverage vs overview.stock_daily(D+1) · D1_DAILY_ROW_MISSING_WITHOUT_TRADABILITY_EVIDENCE · D+1=2026-09-15; daily=VERIFIED_SOURCE; sentiment=PARTIAL; sample=44; valid=43; missing=1; missing_row_codes=['603400']; missing_pct_codes=[] · `{"sample_count": 44, "valid_count": 43, "missing_count": 1, "missing_codes": ["603400"], "missing_row_codes": ["603400"], "missing_pct_codes": [], "data_quality_reasons": ["OUTCOME_PARTIAL"], "daily_status": "VERIFIED_SOURCE"}`
- 2026-09-21 · sentiment.d1_coverage vs overview.stock_daily(D+1) · D1_DAILY_ROW_MISSING_WITHOUT_TRADABILITY_EVIDENCE · D+1=2026-09-22; daily=VERIFIED_SOURCE; sentiment=PARTIAL; sample=82; valid=81; missing=1; missing_row_codes=['002860']; missing_pct_codes=[] · `{"sample_count": 82, "valid_count": 81, "missing_count": 1, "missing_codes": ["002860"], "missing_row_codes": ["002860"], "missing_pct_codes": [], "data_quality_reasons": ["OUTCOME_PARTIAL"], "daily_status": "VERIFIED_SOURCE"}`
- 2026-09-24 · sentiment.limit_up_again vs overview.limit_pool(D+1) · OUTCOME_LIMIT_POOL_DEPENDENCY_UNRESOLVED · unverifiable=39; D+1 up=NOT_COLLECTED trusted=False; limit_up_again=None · `{"unverifiable_count": 39, "overview_status": "NOT_COLLECTED", "overview_trusted": false}`
- 2026-09-29 · sentiment.sample_pool(D up) vs overview.limit_pool(D) · SENTIMENT_SAMPLE_POOL_DEPENDENCY_UNRESOLVED · reasons=['UP_POOL_MISSING']; sample=0; overview.pool_up=NOT_COLLECTED trusted=False · `{"sample_count": 0, "overview_status": "NOT_COLLECTED", "overview_trusted": false}`
- 2026-09-29 · sentiment.metric(D) vs overview.market_sentiment(D) · SENTIMENT_METRIC_DEPENDENCY_UNRESOLVED · METRIC_MISSING; overview.market_sentiment=NOT_COLLECTED · `{"overview_status": "NOT_COLLECTED", "overview_trusted": false}`
- 2026-09-30 · sentiment.sample_pool(D up) vs overview.limit_pool(D) · SENTIMENT_SAMPLE_POOL_DEPENDENCY_UNRESOLVED · reasons=['UP_POOL_MISSING']; sample=0; overview.pool_up=NOT_COLLECTED trusted=False · `{"sample_count": 0, "overview_status": "NOT_COLLECTED", "overview_trusted": false}`
- 2026-09-30 · sentiment.metric(D) vs overview.market_sentiment(D) · SENTIMENT_METRIC_DEPENDENCY_UNRESOLVED · METRIC_MISSING; overview.market_sentiment=NOT_COLLECTED · `{"overview_status": "NOT_COLLECTED", "overview_trusted": false}`

## 已解释差异

- 2026-09-01 · sentiment.d1_coverage vs overview.stock_daily(D+1) · SENTIMENT_SAMPLE_SUBSET_DAILY_PARTIAL · D+1=2026-09-02; daily=PARTIAL; sentiment=VERIFIED; sample=65; valid=65; missing=0; missing_row_codes=[]; missing_pct_codes=[] · `{"sample_count": 65, "valid_count": 65, "missing_count": 0, "missing_codes": [], "missing_row_codes": [], "missing_pct_codes": [], "data_quality_reasons": [], "daily_status": "PARTIAL"}`
- 2026-09-02 · sentiment.d1_coverage vs overview.stock_daily(D+1) · SENTIMENT_SAMPLE_SUBSET_DAILY_PARTIAL · D+1=2026-09-03; daily=PARTIAL; sentiment=VERIFIED; sample=39; valid=39; missing=0; missing_row_codes=[]; missing_pct_codes=[] · `{"sample_count": 39, "valid_count": 39, "missing_count": 0, "missing_codes": [], "missing_row_codes": [], "missing_pct_codes": [], "data_quality_reasons": [], "daily_status": "PARTIAL"}`
- 2026-09-04 · sentiment.d1_coverage vs overview.stock_daily(D+1) · SENTIMENT_SAMPLE_SUBSET_DAILY_PARTIAL · D+1=2026-09-07; daily=PARTIAL; sentiment=VERIFIED; sample=32; valid=32; missing=0; missing_row_codes=[]; missing_pct_codes=[] · `{"sample_count": 32, "valid_count": 32, "missing_count": 0, "missing_codes": [], "missing_row_codes": [], "missing_pct_codes": [], "data_quality_reasons": [], "daily_status": "PARTIAL"}`
- 2026-09-07 · sentiment.d1_coverage vs overview.stock_daily(D+1) · SENTIMENT_SAMPLE_SUBSET_DAILY_PARTIAL · D+1=2026-09-08; daily=PARTIAL; sentiment=VERIFIED; sample=80; valid=80; missing=0; missing_row_codes=[]; missing_pct_codes=[] · `{"sample_count": 80, "valid_count": 80, "missing_count": 0, "missing_codes": [], "missing_row_codes": [], "missing_pct_codes": [], "data_quality_reasons": [], "daily_status": "PARTIAL"}`

## 未解释差异

- 无（仅限本次抽样覆盖的日期）

## 未覆盖日期（未参与本次对账）

- 2026-09-03、2026-09-08、2026-09-11、2026-09-17、2026-09-22、2026-09-28
