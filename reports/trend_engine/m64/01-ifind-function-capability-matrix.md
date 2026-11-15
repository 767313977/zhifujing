# M6.4 Batch 1：iFinD 接口与字段能力矩阵

- 证据：`ifind_function_capability.csv`、`ifind_field_dictionary.csv`、staging `tools_list.json`
- 方法：先 `tools/list` 获取账号真实工具（不消耗行情单元格），再逐个最小查询验证
- 结论：**个股/指数日行情可用；复权字段存在但语义不清；公司行为金额与公告时间缺失；无历史成分接口。**

---

## 1. 账号可见工具（实测）

| Server | 工具数 | 工具 |
| --- | ---: | --- |
| stock | 10 | `get_stock_summary`、`search_stocks`、`get_stock_performance`、`get_stock_info`、`get_stock_shareholders`、`get_stock_financials`、`get_risk_indicators`、`get_stock_events`、`get_esg_data`、`stock_highfreq_quotes` |
| index | 3 | `index_data`、`sector_data`、`index_highfreq_quotes` |

无 SDK 函数（`THS_HistoryQuotes` 等）可调用；实际请求均为 MCP `tools/call`。

## 2. 需求 → 能力

| 需求 | 工具 | 结论 |
| --- | --- | --- |
| 个股 raw 日行情 | `get_stock_performance` | **可用**（含非交易日空行，需过滤） |
| 前/后复权价格 | 同上 | **歧义**：前/后复权都落在同名「收盘价」列 |
| 复权因子 | 同上 | **部分**：有「复权因子」列，除权日变化；无官方字段定义 |
| 现金分红 / 送转 / 配股 | `get_stock_events` | **不可用**：只有日期与进度，金额/比例为空 |
| 公告时间 | `get_stock_events` | **不可用** |
| 六大指数日行情 | `index_data` | **可用** |
| 板块日行情 | `sector_data` | **部分**：区间涨跌幅、算术平均收盘价 |
| 历史板块成分 | 无 | **不可用** |
| 历史 ST / 退市 | 无 | **不可用** |

## 3. 字段与配额

- `get_stock_performance` 返回长表（每行一个指标），列顺序不稳定，必须按列名取值；
- 数值带单位后缀（`万`、`亿`），必须显式换算；
- `index_data` 同样含非交易日空行；
- 每次 `tools/call` 按调用次数计量；本轮全部串行。

## 4. Batch 1 门禁

| 门禁 | 结果 |
| --- | --- |
| 工具名来自真实 `tools/list` | 通过 |
| 字段字典有真实样本 | 通过 |
| 复权字段定义可解释 | **未通过**（见 `02`） |
| 公司行为金额/公告时间 | **不可用** |
