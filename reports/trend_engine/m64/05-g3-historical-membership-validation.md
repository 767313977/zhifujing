# M6.4 Batch 4：G3 历史板块成分验证

- 证据：`g2_g3_validation.json`、staging `tools_list.json`
- 结论：**G3 FAIL**。账号工具集中没有历史成分接口，未取得任何成分数据，也没有用当前成分回填历史。

---

## 1. 工具能力

| Server | 工具 | 是否支持成分 |
| --- | --- | --- |
| index | `index_data` | 否 |
| index | `sector_data` | 否（只有行情/估值/财务指标） |
| index | `index_highfreq_quotes` | 否 |
| stock | 10 个工具 | 均不含板块成分 |

结论：无 `membership` / `constituents` / `in_date/out_date` 类接口。

---

## 2. 未执行项

| 计划 | 状态 |
| --- | --- |
| 5 个高频变动板块的 ≥3 个历史截面 | `NOT_SUPPORTED` |
| in/out 区间 | `NOT_SUPPORTED` |
| snapshot-only 标记 | `NOT_SUPPORTED` |
| 退市证券是否在成分内 | 无法验证 |
| 代码变更/重复/无效代码核对 | 无法验证 |

---

## 3. 判定

- G3 **FAIL**；
- 2026-07-03 之前的板块特征继续保持 `UNKNOWN`；
- 不得用 b1 当前成分或 iFinD 当前板块列表回看历史。

如需解除，必须接入能提供「日期 + 成分列表」或「纳入/剔除日期」的数据源，并先做 5 个板块 × 3 个截面的样本验证。
