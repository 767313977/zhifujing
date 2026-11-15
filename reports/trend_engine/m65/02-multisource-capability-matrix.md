# M6.5 Batch 2：多数据源候选能力矩阵（仅公开文档调查）

- 日期：2026-11-15
- 调查顺序：**Tushare Pro → 理杏仁 → CnOpenData → Choice**（JoinQuant 仅列备选，iFinD 作对照）
- 授权状态：**全部 `NOT_AUTHORIZED`**。本批次**未注册、未购买、未使用任何 Token、未调用任何第三方接口、未下载任何数据**。
- 证据：`02-multisource-capability-matrix.csv`（26 行 × 17 列）
- 状态取值：`DOCUMENTED` / `ACCOUNT_CONFIRMED` / `SAMPLE_VERIFIED` / `NOT_AVAILABLE` / `UNKNOWN`

---

## 0. 一页结论

| Gate | 最可行候选 | 现实阻塞 |
| --- | --- | --- |
| **G9 价格子项** | Tushare `adj_factor` + `daily`（官方口径、按日快照） | 需 2000 积分（200 元/年）；因子是否回溯修订未说明 |
| **G9 交易会计子项** | 理杏仁 `cn/company/dividend` + `cn/company/allotment` + `cn/company/equity-change` | 有每股派现/送转/配股比例与价格，但**公告日期仅日期级**；API 价格与配额未公开；许可限内部使用 |
| **G3 历史板块成分** | Tushare `index_member_all`（申万，含 in/out）；Choice `em_sector(isHistory=1)` + 终端「成份变动记录」 | Tushare 仅申万分类，与 b1 的 kph 自建分类需映射；`dc_member` 历史**仅 2024-12-20 起**；Choice 历史成分仅沪深股票/上交所期权，且许可禁止复制 |
| **G2 板块日线** | Tushare `dc_index`（板块日快照） | 6000 积分门槛；非标准 OHLCV |
| **网站商用许可** | CnOpenData 商业分析版（唯一明示「商业使用授权」） | 年度更新，时效不满足日常研究；价格需询价 |

**一句话**：Tushare 能补价格与部分会计，理杏仁能补会计明细，Choice 最接近 G3，但**没有一家能在现有授权下直接落地**——全部需要用户单独批准账号/采购/许可。

---

## 1. 方法与证据分级

| 级别 | 含义 | 本批次使用情况 |
| --- | --- | --- |
| 一手官方文档 | 供应商官网接口文档/协议原文 | 绝大多数结论（Tushare 文档站、理杏仁开放平台、CnOpenData 目录页、Choice 帮助中心与接口手册） |
| 官方文档（未展开核对） | 目录/索引页可见，但字段未逐项核对 | Choice 公司行为指标、JoinQuant 许可条款 |
| 二手线索 | 第三方文章/镜像，仅作线索 | Tushare `adj_factor` 为「累计后复权因子」的说法来自第三方解读（腾讯云开发者社区），**官方文档未明示方向** |
| 实测 | 本项目实际调用得到的证据 | 仅 iFinD（M6.4） |

所有 `UNKNOWN` 表示「公开文档未覆盖」，不是「不支持」。

---

## 2. Tushare Pro（第一优先）

### 2.1 接口能力

| 接口 | 结论 | 依据 |
| --- | --- | --- |
| `adj_factor` | 返回 `ts_code / trade_date / adj_factor`，可按单票全历史或单日全市场取；盘前 09:15~09:20 完成当日入库；2000 积分起 | https://tushare.pro/document/2?doc_id=28 |
| `daily` | 未复权 OHLCV，15~16 点入库；`pre_close` 为除权后昨收 | https://tushare.pro/document/2?doc_id=27 |
| `dividend` | 含 `ann_date`、`div_proc`（实施进度）、`stk_div`、`stk_bo_rate`、`stk_co_rate`、`cash_div`（税后）、`cash_div_tax`（税前）、`record_date`、`ex_date`、`pay_date`、`div_listdate`、`imp_ann_date`、`base_date`、`base_share`；起始 2000-01-01，单次 2000 行，每日 20~21 点更新，2000 积分 | https://tushare.pro/document/2?doc_id=103 |
| `dc_member` | 按 `trade_date + ts_code(板块代码)` 取每日成分；**历史起始 2024-12-20**；单次 5000 行；6000 积分 | https://tushare.pro/document/2?doc_id=363 |
| `dc_index` | 板块日快照（涨跌幅、换手、上涨/下跌家数、总市值）；`idx_type` 区分行业/概念/地域；6000 积分 | https://tushare.pro/document/2?doc_id=362 |
| `index_member_all` | 申万三级成分，含 **`in_date` / `out_date` / `is_new`**，示例中含退市证券（`退市刚泰`、`*ST荣华(退市)`）；2000 积分，单次 2000 行 | https://tushare.pro/document/2?doc_id=335 |
| `ths_member` | 仅「最新的概念板块成分」，`in_date` 标注「暂无」→ **无历史** | https://tushare.pro/document/2?doc_id=261 |
| `stock_basic` | `list_status` 支持 L/D/P/G/UN，含 `delist_date` → 可用于退市覆盖核对 | https://tushare.pro/document/2?doc_id=25 |
| 配股 | **未发现专用接口**（`stk_seasoned` 为增发，非配股） | 接口清单 https://tushare.pro/document/2 |

### 2.2 权限与价格

| 积分档 | 频次 | 日总量 | 价格 |
| --- | --- | --- | --- |
| 120 | 50 次/分钟 | 8000 次 | 0 元 |
| 2000 以上 | 200 次/分钟 | 10 万次/API | 200 元/年 |
| 5000 以上 | 500 次/分钟 | 常规数据无上限 | 500 元/年 |
| 10000 以上 | 500 次/分钟 | 特色数据 300 次/分钟 | 1000 元/年 |

（https://tushare.pro/document/2?doc_id=290 ；机构价格为个人 10 倍，购买不退款。）

`dc_member` / `dc_index` / `ths_member` 要求 **6000 积分**，高于 5000 元/年档位，实际门槛需在账号内确认（`UNKNOWN`）。

### 2.3 许可（关键阻塞）

服务协议原文：「本服务是在您遵守本协议……的前提下，Tushare 给予您一项**个人的、不可转让的、非商业用途的、可撤销的、有期限及非排他性的许可**……**仅可为非商业目的使用，并仅可用作个人查看使用**。」
（https://tushare.pro/document/1?doc_id=405）

推论：Tushare 数据原则上**不能用于 zhifujing 网站的对外展示**，也不能转售/再分发。这是 G3/G9 采用 Tushare 的最大合规障碍，必须由用户与供应商另行确认。

### 2.4 与本项目的匹配度

- G9 价格：`daily` + `adj_factor` 可独立重建复权序列，且**方向口径单一（累计因子）**，正好补 iFinD 的字段歧义。
- G9 会计：`dividend` 覆盖现金分红与送转，**缺配股**；公告时间只有 `ann_date`（日期级），PIT 只能做到 date-level。
- G3：`index_member_all` 有 in/out，但只有申万体系；`dc_member` 历史仅 2024-12-20 起。
  - 实测 b1 概念数据区间为 2025-03-05~2026-09-28，**理论上落在 `dc_member` 覆盖范围内**；但 b1 的 374 个板块（270 概念/精选 + 104 行业，taxonomy 为 `kph_*` 自建分类）与东财 BK 代码需先做映射。
- 结论：**G9 价格子项最可能由 Tushare 解锁；G3 只能做「近 14 个月 + 映射后」的部分验证。**

---

## 3. 理杏仁（第二优先）

### 3.1 接口能力

| 接口 | 字段 | 依据 |
| --- | --- | --- |
| `POST /api/cn/company/dividend` | `date`（公告日期）、`content`、`bonusSharesFromProfit`（送股）、`bonusSharesFromCapitalReserve`（转增）、`dividend`（分红）、`currency`、`dividendAmount`、`marketDividendAmount`、`annualNetProfit`、`annualNetProfitDividendRatio`、`registerDate`、`exDate`、`paymentDate`、`fsEndDate` | https://www.lixinger.com/api/open-api/html-doc/cn/company/dividend |
| `POST /api/cn/company/allotment` | `date`（公告日期）、`exDate`、`currency`、`allotmentRatio`（配股比例）、`allotmentPrice`（配股价）、`allotmentShares` | https://www.lixinger.com/api/open-api/html-doc/cn/company/allotment |
| `POST /api/cn/company/equity-change` | 股本变动日期、公告日期、变动原因、总股本/流通/限售等 | https://www.lixinger.com/api/open-api/html-doc/cn/company/equity-change |
| 行业 | 仅行业分类元数据（sw / sw_2021 / cni 的代码、名称、级别、发布日期），**无成分列表** | https://www.lixinger.com/api/open-api/html-doc/cn/industry |
| 复权因子 | 开放平台目录中**未见**复权因子/前复权序列接口 | https://www.lixinger.com/open/api |

### 3.2 语义细节（重要）

1. `date` 官方标注为「公告日期」，示例返回 `"2026-07-25T00:00:00+08:00"` → **日期级，不是精确发布时间**，不得当 timestamp 用。
2. 同一事件可返回**多行**：300750 在 `2026-03-10` 同时有 `dividend=4.779` 与 `dividend=2.178` 两行（同一登记日/除权日）。做账本时必须按方案聚合，不能让两行各算一次现金流。
3. 无「方案状态机」字段（预案/股东大会/实施），只有 `content` 文本 + 公告日期。
4. 单次查询区间 ≤10 年。

### 3.3 价格、配额与许可

- API 价格与调用配额：**公开页未提供**（`UNKNOWN`）。网站订阅价（年付 518 元等）是终端产品价，**不等于 API 价**，不可混用。
- 用户协议 4.6：「所有信息和数据……均为本公司的商业信息……用户只能**在您（个人或单一机构团体）范围内使用**，更**不得传播至您（个人或单一机构团体）以外的范围，或者用于商业用途**。」
  （https://www.lixinger.com/marketing/user-agreement）
- 结论：许可与 Tushare 类似，**网站展示不可直接使用**。

### 3.4 与本项目的匹配度

- G9 会计：**四家候选里字段最完整**——分红、送转、配股（比例+价格）、股本变动齐备，且公告日期可查。
- 缺点：无复权因子（价格子项不能靠它）、公告仅日期级、事件多行需去重规则、许可限内部。
- G3：无成分接口，**不能用于 G3**。

---

## 4. CnOpenData（第三优先）

### 4.1 复权行情

- 表结构仅 3 字段：`股票代码 / 交易日期 / 复权因子`；区间 1991-12-19~2026-07-22。
- 目录页描述「配备精确的复权因子，支持用户灵活生成前复权、后复权价格序列」，但**未给出前/后复权的字段区分方式**，也未见单次因子与累计因子分列。
- 更新频率：**年度更新**（页面明示）。
- 依据：https://www.cnopendata.com/data/m/listedcompany-basic/A-market-quotes.html

### 4.2 分红送股

- 18 字段：证券代码、公司简称、分送日期、**预案公告日**、实施进度、每股送转、每股送股比例、每股转增比例、每股分红（税后）、每股分红（税前）、股权登记日、除权除息日、派息日、红股上市日、**实施公告日**、基准日、基准股本（万）、**是否变更过**。
- 区间 1990~2025-06-30，**年度更新**。
- 依据：https://www.cnopendata.com/data/m/listedcompany-basic/listedco-dividend.html

### 4.3 交付与授权

- 三档：商业分析版（完整历史 + **API 接口** + 商业使用授权）、学术研究版（CSV/Stata/Excel + 学术引用授权）、企业定制版（本地部署）。
- 价格全部「联系销售」，无公开报价。
- 板块/概念历史成分：**目录中未检索到对应数据集**（`UNKNOWN`）。

### 4.4 与本项目的匹配度

- 唯一明示「商业使用授权」的候选，最契合网站场景；但**年度更新**无法支撑日常研究（单日因子/事件会滞后数月）。
- 无板块成分证据 → 对 G3 无帮助。
- 结论：**不适合作为主数据源，可作为历史回溯/校验的离线备选。**

---

## 5. Choice（第四优先）

### 5.1 历史板块成分

- 终端帮助页：「选择所需查询的板块，默认为最新成份……点击日历标志选择对应时间截点，点击【查询】，结果为所选板块对应时间截点的**历史成份**。选择时间区间查看板块成份**纳入剔除**情况……系统日往前一年作为默认时间区间。」
  （https://choice.eastmoney.com/School/Help）
- 量化接口 `em_sector(pukeycode, enddate, options)`：「获取 Choice 金融终端指定系统板块证券代码成分列表，**目前只支持沪深股票、上交所期权的历史成分查询，其他板块只能获取最新成分**」；参数 `isHistory=0/1`。
  （https://quantapi.eastmoney.com/Upload/EMQuantAPI_R.html 、Java 手册附注 8）

### 5.2 许可

- 产品最终用户许可协议 3.2.3：「未经东方财富信息股份有限公司**书面同意**，用户不得擅自实施包括但不限于下列行为：使用、复制、修改、链接、转载、汇编、发表、出版，建立镜像站点、擅自借助产品发展与之有关的衍生产品、作品、服务等。」
  （https://choice.eastmoney.com/html/userprotocol/userprotocol.html）
- 程序化使用需账号激活（绑定手机号 + 令牌文件），套餐权限与价格不公开。

### 5.3 与本项目的匹配度

- 是四家中**唯一同时具备「历史截面」与「纳入/剔除记录」**的产品形态，最贴近 G3 定义。
- 但：历史成分 API 明确仅沪深股票/上交所期权；b1 的 374 个板块是 `kph_*` 自建分类，需与 Choice 系统板块做人工映射；许可禁止复制/镜像/衍生，网站展示需书面授权。
- 结论：**G3 的商业备选，但必须采购 + 书面许可 + 人工映射，且非全板块可查。**

---

## 6. JoinQuant（备选，仅在四家均不满足时启用）

- `get_index_stocks` / `get_industry_stocks` / `get_concept_stocks` 均支持传 `date` 取历史成分（https://cdn.joinquant.com/help/img/JoinQuantAPI.pdf 、https://pypi.org/project/jqdatasdk/1.5.5/）。
- 导出与许可条款**未核对**（`UNKNOWN`）：SDK 函数存在 ≠ 有权导出或商用。
- 本批次不深入调查，避免分散资源。

---

## 7. iFinD 对照（不新增取数）

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 复权因子存在 | `SAMPLE_VERIFIED` | M6.4 实测 600519 因子 8.6760→8.8814 |
| 前/后复权可区分 | `NOT_AVAILABLE` | 同名列，无方向字段 |
| 公司行为金额/公告时间 | `NOT_AVAILABLE` | 仅日期与进度 |
| 历史板块成分 | `NOT_AVAILABLE` | 13 个工具中无 |
| 板块日线 | `SAMPLE_VERIFIED`（部分） | 区间统计，非标准日 K |

---

## 8. 对 Gate 的净影响

| Gate | 现状（M6.4） | 本批次新增判断 | 是否可推进 |
| --- | --- | --- | --- |
| G9 价格 | FAIL | Tushare `adj_factor + daily` 可提供方向明确的累计因子；理杏仁/CnOpenData 可作交叉源 | 需授权后做样本验证 |
| G9 会计 | FAIL | 理杏仁字段最全（分红+送转+配股+股本），Tushare 缺配股，CnOpenData 有全字段但年度更新 | 需授权后做样本验证 |
| G3 | FAIL | Tushare `index_member_all`（申万 in/out）、Choice（历史截面+变动记录）、`dc_member`（2024-12-20 起）三条路径 | 需授权 + 板块映射 |
| G2 | PARTIAL | Tushare `dc_index` 可提供板块日快照 | 需 6000 积分 |
| 网站商用 | 未评估 | 仅 CnOpenData 商业版明示可商用；Tushare/理杏仁/Choice 均限制 | 需用户决策 |

---

## 9. 事实性更正（供最终报告引用）

- 任务书表述「383 个 b1 板块」：实测 `sector_daily` 去重板块数为 **383**；`sector_basic` 注册板块为 **374**（270 `kph_selected` + 104 `kph_industry`）。两者不一致，批量映射时应以 `sector_basic` 为准并说明差异。
- b1 自有成分数据只覆盖 **2026-07-03~2026-09-24（60 个交易日、275 个板块）**，本身不足以支撑历史截面验证 → G3 必须依赖外部供应商。

## 10. 授权声明

本批次全部结论来自公开文档与本地只读查询（b1 表结构/计数），**未发生**：供应商注册、试用申请、套餐购买、Token 使用、第三方接口调用、数据下载。所有新增账号与费用均需用户在 Batch 7 审批表中逐项批准。
