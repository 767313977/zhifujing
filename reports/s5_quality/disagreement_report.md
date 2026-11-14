# S5 数据质量对账（只读、抽样）

- 生成时间：2026-11-14T22:13:51
- Git：`0c182d263274697f4d6acf2d02d9825b55703b55`（分支 `fix/s5-second-review`，工作区 dirty=False）
- 命令：`scripts/check_s5_quality_consistency.py --db backend/data/b1_research.db --start 2026-09-01 --end 2026-09-30 --sample 15 --out reports/s5_quality/contract_matrix.json --md reports/s5_quality/disagreement_report.md`
- 环境：Python 3.13.13 / Node v24.19.0 / SQLite 3.50.4
- 退出码：2
- 数据库：`b1_research.db`（ro&immutable=1）
- 区间：2026-09-01 ~ 2026-09-30
- 矩阵版本：`quality-consistency-matrix-v2`

## 抽样口径

- 策略：等距抽样：--sample >= 区间交易日数时全量；否则按 (n-1)/(sample-1) 均匀取索引，样本唯一递增并覆盖区间首尾
- 请求样本：15；区间交易日：21；实际抽样：15
- 已抽日期：2026-09-01、2026-09-02、2026-09-04、2026-09-07、2026-09-09、2026-09-10、2026-09-14、2026-09-15、2026-09-16、2026-09-18、2026-09-21、2026-09-23、2026-09-24、2026-09-29、2026-09-30
- 覆盖率：0.7143；是否全历史：False
- 说明：抽样对账不等于全历史完整验收；未覆盖日期见 uncovered_days

## 结果

- 检查总数：60
- 通过：51
- 已解释差异：4
- 未解释差异：5
- SQL：1258；耗时：2391.8 ms

## 未解释差异

- 2026-09-14 · overview.stock_daily vs sentiment-next-day · next_day=2026-09-15; daily=VERIFIED_SOURCE; sentiment=PARTIAL; sample=44; valid=43; missing=1
- 2026-09-21 · overview.stock_daily vs sentiment-next-day · next_day=2026-09-22; daily=VERIFIED_SOURCE; sentiment=PARTIAL; sample=82; valid=81; missing=1
- 2026-09-24 · overview.stock_daily vs sentiment-next-day · next_day=2026-09-28; daily=VERIFIED_SOURCE; sentiment=PARTIAL; sample=39; valid=39; missing=0
- 2026-09-29 · overview.stock_daily vs sentiment-next-day · next_day=2026-09-30; daily=VERIFIED_SOURCE; sentiment=UNVERIFIED; sample=0; valid=0; missing=0
- 2026-09-30 · overview.stock_daily vs sentiment-next-day · next_day=2026-10-08; daily=VERIFIED_SOURCE; sentiment=UNVERIFIED; sample=0; valid=0; missing=0

## 未覆盖日期（未参与本次对账）

- 2026-09-03、2026-09-08、2026-09-11、2026-09-17、2026-09-22、2026-09-28
