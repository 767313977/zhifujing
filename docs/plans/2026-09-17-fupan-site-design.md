# 每日复盘与选股网站 — 设计文档

- 日期：2026-09-17
- 定位：**个人自用**、本地运行、**A 股**为主
- 技术方案：FastAPI + SQLite + React（方案 ①）
- 数据源方案：**iFinD 主力 + akshare 补缺**（方案 A）

---

## 1. 环境与数据源验证结论

### 1.1 环境

| 项 | 版本 | 结论 |
|---|---|---|
| Python | 3.13.13 | 可用 |
| Node / npm | 24.19.0 / 11.17.0 | 可用 |
| git | 2.55.0 | 可用 |
| akshare | 1.18.96 | 安装成功，Python 3.13 兼容 |
| pandas | 3.0.5 | 可用 |
| fastapi | 0.141.1 | 可用 |

### 1.2 iFinD 接入方式

- 协议：**JSON-RPC over HTTP（MCP Streamable HTTP）**
- 端点：`https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-{stock,index,fund,edb,news,bond,global-stock,futures}-mcp`
- 鉴权：请求头 `Authorization: <IFIND_AUTH_TOKEN>`
- 会话：先 `initialize` 拿 `Mcp-Session-Id`，发 `notifications/initialized`，再 `tools/call`
- **后端可直接调用**，参考实现见 [probe_ifind.py](../../scripts/probe_ifind.py)
- 密钥存放：项目根目录 `.env` 的 `IFIND_AUTH_TOKEN`（已在 `.gitignore` 中，不入库）

### 1.3 iFinD 实测可用工具

用 [probe_ifind.py](../../scripts/probe_ifind.py) 拉取真实清单（非文档通用版）：

| 服务 | 工具数 | 工具 |
|---|---|---|
| `stock` | 10 | search_stocks, get_stock_summary, get_stock_performance, get_stock_info, get_stock_shareholders, get_stock_financials, get_risk_indicators, get_stock_events, get_esg_data, stock_highfreq_quotes |
| `index` | 3 | index_data, sector_data, index_highfreq_quotes |
| `news` | 3 | search_news, search_notice, search_trending_news |

### 1.4 关键实测结果

| 场景 | 调用 | 结果 | 耗时 |
|---|---|---|---|
| 指数实时 + 涨跌停家数 | `index.index_highfreq_quotes` | ✅ 上证 涨1062/跌1205/涨停28/跌停0 | 1.7s |
| 智能选股 | `stock.search_stocks` | ✅ "电子行业市值>500亿且涨幅>3%" → 9 只 | 0.7s |
| 个股实时快照 | `stock.stock_highfreq_quotes` | ✅ 含量比/换手/市值/PE | 0.6s |
| 个股历史日频 | `stock.get_stock_performance` | ✅ 近 20 交易日收盘价/涨跌幅/成交额 | 3.0s |
| 板块行情 | `index.sector_data` | ✅ 涨跌幅 + 成分股数 + 成交额 | 4.1s |

**交叉校验通过**：`stock_highfreq_quotes` 贵州茅台收盘价 1266.98 / 涨跌幅 0.7138，
与 `get_stock_performance` 的 20260917 数据完全一致。

### 1.5 iFinD 的坑（设计必须处理）

1. **`symbols` 单次上限 10 只，超出静默丢弃且不报错**
   实测传 11 只 → 只返回 10 只，第 11 只（中信证券）无声消失。必须自行分片。
2. **自然语言工具返回 Markdown 表格字符串，不是结构化数组**
   `search_stocks` / `get_stock_performance` / `sector_data` 都在 `data.answer` 里塞一张
   或**多张** Markdown 表。需要写 Markdown 表格解析器。
3. **返回结果混入非交易日**
   实测要 20 个交易日 → 返回 29 行，含周末；非交易日的涨跌幅/成交额为空 `\t`，
   收盘价为前一交易日填充值。必须按交易日历过滤。
4. **表头单位与数值单位不一致**
   表头写 `成交额（单位：元）`，值是 `22.1734亿`。解析需按"亿/万"自行换算。
5. **板块代码体系混用**
   `sector_data` 对"半导体"返回**中信行业分类**（`001033206010`），
   对"证券"返回**同花顺行业类**（`00103512701`）。口径不统一，必须在 query 中显式指定分类体系。
6. **全市场涨跌停家数不完整**
   `index_highfreq_quotes` 的涨跌停家数只对**上证指数**和**深证成指**有值；
   **深证综指、中证全指返回 `null`**。而深证成指仅含 500 只成分股，
   导致深市涨停家数被严重低估。**全市场涨停口径只能靠 akshare 涨停池**。
7. **限流严格且报错方式隐蔽**（P1 实测补充）
   - 权益：免费版 2 请求/秒、个人版 5、企业版 10
   - **突发会触发 429**：一次性放行 4 个请求即返回
     `{"answer":"用户请求过于频繁，请稍后重试"}`
   - **429 时 HTTP 状态码仍是 200**，错误藏在工具结果文本里。若只在
     HTTP 层判错，429 会被误当成 JSON 解析失败而**不重试**
   - 对策：令牌桶**不预留突发额度**（capacity=1，请求按 1/rate 秒均匀放出），
     并在重试范围内按文本识别限流

### 1.6 iFinD 的能力缺口（由 akshare 补齐）

grep iFinD 全部 9 个参考文档，`涨停|跌停|龙虎榜|资金流|连板|封板` **仅命中 1 行**
（指数级"涨停家数/跌停家数"）。以下**iFinD 完全没有**，而它们是 A 股复盘的核心：

| 缺口 | akshare 接口 | 实测 |
|---|---|---|
| 涨停池（含连板数、封板资金） | `stock_zt_pool_em` | ✅ 89 条 |
| 跌停池 | `stock_zt_pool_dtgc_em` | ✅ 4 条 |
| 炸板池 | `stock_zt_pool_zbgc_em` | ✅ 11 条 |
| 龙虎榜 | `stock_lhb_detail_em` | ✅ 73 条 |
| 交易日历 | `tool_trade_date_hist_sina` | ✅ 8797 条 |
| **全市场涨跌家数** | `stock_market_activity_legu` | ✅ 见下 |

**全市场涨跌家数（乐咕乐股）**：iFinD 的涨跌家数只对上证指数有效
（深证A指/国证A指/中证全指均返回 `null`），而深证成指仅含 500 只成分股。
改用 `stock_market_activity_legu`，**一次调用**给出全市场宽度，且该接口走乐咕乐股、
与东财集群无关，不受频控影响：

```
上涨 2503 / 下跌 2558 / 平盘 148 / 停牌 12 / 活跃度 47.94%
涨停 50（真实涨停 47 + ST涨停 3）/ 跌停 2
```

**交叉验证通过**：乐咕的「真实涨停 47」与 akshare 涨停池的 47 条完全吻合，
两个独立数据源互证。

### 1.7 akshare 侧的已知问题

`push2*.eastmoney.com` 集群（全 A 快照、板块列表、个股资金流、个股日线）会因请求频率
触发**本机 IP 临时频控**（实测连接级失败，等待 75s 未恢复）。
`push2ex.eastmoney.com`（涨停池/龙虎榜）是独立集群，不受影响。

因此：**只用 akshare 的 `push2ex` 系列接口**（涨停池/跌停池/炸板池/龙虎榜），
不碰 `push2` 系列。全 A 快照类需求由 iFinD 承担。

### 1.8 备用源（已实测可用，作为最后兜底）

| 源 | 地址 | 特点 |
|---|---|---|
| 新浪实时 | `hq.sinajs.cn/list=...` | 支持批量，单次数百只 |
| 腾讯实时 | `qt.gtimg.cn/q=...` | 支持批量 |

---

## 2. 架构

```
fupan/
├── .env                          # IFIND_AUTH_TOKEN（gitignored）
├── scripts/
│   ├── probe_akshare.py          # akshare 健康探测
│   └── probe_ifind.py            # iFinD 工具清单 + 单次调用探测
├── backend/
│   ├── app/
│   │   ├── main.py               # FastAPI 入口 + 静态托管前端产物
│   │   ├── config.py             # 配置（限速、路径、数据源开关）
│   │   ├── db.py                 # SQLAlchemy engine / session
│   │   ├── models.py             # ORM 模型
│   │   ├── schemas.py            # Pydantic 响应模型
│   │   ├── api/                  # 路由层
│   │   ├── sources/              # 数据源适配层（隔离外部接口脆弱性）
│   │   │   ├── base.py           # 令牌桶限速 / 退避重试 / 分片
│   │   │   ├── ifind.py          # iFinD MCP 客户端
│   │   │   ├── akshare_source.py # 涨停三池 / 龙虎榜 / 交易日历 / 市场活跃度
│   │   │   └── markdown_table.py # iFinD Markdown 表格与数值解析
│   │   ├── services/             # 业务计算（情绪指标、选股条件编译）
│   │   └── jobs/
│   │       ├── collect_daily.py  # 采集 + 回补 + 情绪计算
│   │       └── scheduler.py      # APScheduler
│   ├── data/fupan.db             # SQLite
│   └── requirements.txt
└── frontend/                     # React + Vite + TS + Tailwind + ECharts
```

**数据流：**

```
iFinD MCP ─┐
           ├─(限速/重试/分片/降级)─> sources ─> services(解析&计算) ─> SQLite
akshare   ─┘                                                          │
                                                         FastAPI 只读 ──> React
```

**两个关键约束：**

1. **采集与页面彻底解耦**：页面只读 SQLite，绝不实时拉外部接口。
2. **外部脆弱性锁死在 `sources/` 一层**：接口变更、限流策略调整都只改这层。

### 2.1 数据源职责划分

| 数据 | 主源 | 兜底 |
|---|---|---|
| 指数实时（涨跌停家数） | iFinD `index_highfreq_quotes` | — |
| 指数历史 | iFinD `index_data` | — |
| 板块行情 + 成分股数 | iFinD `sector_data` | — |
| 个股实时快照（自选/选股结果） | iFinD `stock_highfreq_quotes` | 新浪 / 腾讯 |
| 个股历史日频 | iFinD `get_stock_performance` | — |
| **智能选股** | iFinD `search_stocks` | — |
| 新闻公告 | iFinD `search_news/notice` | — |
| **涨停池 / 跌停池 / 炸板池** | akshare `push2ex` | — |
| **龙虎榜** | akshare `push2ex` | — |
| **全市场涨跌家数** | akshare 乐咕乐股 | — |
| 交易日历 | akshare `tool_trade_date_hist_sina` | — |

三层降级链（个股快照）：iFinD `stock_highfreq_quotes` → 新浪 `hq.sinajs.cn` → 腾讯 `qt.gtimg.cn`。
新浪与腾讯都支持批量，是最后的兜底手段。

---

## 3. 数据模型（SQLite）

| 表 | 主键 | 说明 |
|---|---|---|
| `trade_calendar` | trade_date | 交易日历（用于过滤 iFinD 返回的周末行） |
| `index_daily` | trade_date, code | 指数日线 + 上涨/下跌/涨停/跌停家数 |
| `stock_basic` | code | 名称、市场、板块、上市日、总市值、流通市值、行业 |
| `stock_daily` | trade_date, code | 日线：OHLC、涨跌幅、成交量额、换手率、量比 |
| `limit_pool` | trade_date, code, pool_type | 涨停/跌停/炸板：封板资金、首次封板时间、开板次数、连板数 |
| `sector_daily` | trade_date, sector_code | 板块行情：涨跌幅、成交额、成分股数 |
| `sector_member` | sector_code, stock_code | 板块成分股 |
| `lhb` | trade_date, code, reason | 龙虎榜：净买额、买入额、卖出额、上榜原因 |
| `market_sentiment` | trade_date | **物化**情绪快照（见下） |
| `watchlist` | code | 自选股 + 备注 + 标签 |
| `review_note` | trade_date | 复盘笔记：市场观点、次日计划 |
| `screen_preset` | id | 保存的选股条件（自然语言 + 结构化条件） |
| `screen_result` | id, code | 选股结果快照 |
| `collect_log` | id | 采集日志：任务、状态、耗时、错误 |

**`market_sentiment` 物化字段**（情绪曲线要快速读 60 天，实时计算太慢）：

`limit_up_count`、`limit_down_count`、`broken_count` 炸板数、`seal_rate` 封板率、
`broken_rate` 炸板率、`max_consecutive` 最高连板、`up_count`/`down_count` 涨跌家数、
`total_amount` 成交额、`yesterday_limit_today_avg` 昨日涨停股今日均涨幅（打板赚钱效应）。

> 注意：`limit_up_count` 等**以 akshare 涨停池为准**，不用 iFinD 指数级数据
> （见 1.5 第 6 条，iFinD 深市口径严重低估）。

---

## 4. 采集流程

**触发时机：** 交易日 15:05 后（收盘数据已稳定）+ 支持手动补数。

顺序（每步独立事务、独立日志、失败不阻塞后续）：

1. 交易日历（akshare）
2. 指数日线 + 涨跌家数（iFinD `index_highfreq_quotes` / `index_data`）
3. 涨停池 / 跌停池 / 炸板池（akshare `push2ex`）
4. 板块行情 + 成分股（iFinD `sector_data`）
5. 龙虎榜（akshare `push2ex`）
6. 自选股 + 最近选股结果的个股快照（iFinD `stock_highfreq_quotes`，按 10 只分片）
7. 计算并写入 `market_sentiment`

> **不再做全市场日线落库。** 原先 akshare 方案要拉 ~5400 只股票（触发频控），
> 而现在"找票"由 iFinD `search_stocks` 直接承担，无需本地全市场库。
> 本地只存**自选股 + 选股结果**的个股日线，数据量下降两个数量级。

**限速策略：**

- iFinD：个人版 4 req/s（上限 5，留余量），**令牌桶不预留突发额度**
- akshare（push2ex）：1 req/s
- 失败退避：2s → 4s → 8s，最多 3 次
- **`stock_highfreq_quotes` 必须按 10 只分片**（超出会静默丢弃）

### 4.1 历史回补能力边界（P1 实测）

部分数据源**只提供当日值**，对历史日期取数会把今天的数据写到过去。
这类静默的数据污染比直接失败更危险，因此在代码里**硬性拦截**（`_require_today`）。

| 数据 | 可否回补 | 原因 |
|---|---|---|
| 涨停 / 跌停 / 炸板池 | ✅ | akshare 接口按日期取数 |
| 龙虎榜 | ✅ | 同上 |
| 情绪指标：涨停数、跌停数、炸板数、封板率、炸板率、最高连板 | ✅ | 由三池推导 |
| 指数快照 | ❌ | iFinD 高频接口仅支持当日 |
| 涨跌家数 | ❌ | 乐咕乐股仅返回当日 |
| 打板效应（昨日涨停股今日均涨幅） | ❌ | 依赖当日实时涨幅 |

**结论**：情绪周期最核心的指标（涨停数、封板率、炸板率、连板高度）**全部可回补**，
所以 60 日情绪曲线可以往前补。不可回补的三项在历史记录里留空。

回补 13 个交易日的实测结果（零失败）：

```
日期        涨停  跌停  炸板  封板率   炸板率  连板
2026-09-04   39    9    48   44.83   55.17    5    ← 冰点
2026-09-07   93    2    19   83.04   16.96    6    ← 高潮
2026-09-16   89    4    11   89.00   11.00    6    ← 又一高潮
2026-09-17   47    1    20   70.15   29.85    5    ← 回落
```

---

## 5. API

```
GET  /api/market/overview?date=          大盘卡片 + 情绪指标
GET  /api/market/sentiment?days=60      情绪周期曲线
GET  /api/limit/pool?date=&type=up|down|broken   涨停梯队
GET  /api/sector/rank?date=              板块排序
GET  /api/stock/{code}/daily?days=120    K 线
GET  /api/stock/{code}/profile           个股概况
POST /api/screener/nl                    自然语言选股（iFinD search_stocks）
POST /api/screener/run                    结构化条件选股（本地库）
GET  /api/screener/fields                条件字段元数据
GET/POST/DELETE /api/watchlist           自选股
POST /api/note/{date}                    复盘笔记
GET/POST /api/screen-preset              保存的选股条件
POST /api/admin/collect                  手动补数
GET  /api/admin/status                   采集状态
```

---

## 6. 前端页面

| 路由 | 页面 | 核心内容 |
|---|---|---|
| `/` | 今日复盘 | 指数卡片、成交额、涨跌家数、情绪温度、涨停梯队、板块热力、自选股表现 |
| `/sentiment` | 情绪周期 | 近 60 日涨停数 / 炸板率 / 连板高度 / 指数叠加曲线 |
| `/sectors` | 板块题材 | 板块排行、板块内个股、板块历史强度 |
| `/limit-up` | 涨停复盘 | 按连板高度分层，标注所属题材 |
| `/screener` | 选股器 | **自然语言选股（主力）** + 结构化条件筛选 + 保存条件 |
| `/watchlist` | 自选股 | 自选池、每日表现、笔记 |
| `/stock/:code` | 个股详情 | K 线 + 均线 + 量能、所属板块、龙虎榜、财务摘要 |
| `/settings` | 数据管理 | 采集状态、手动补数、日志 |

---

## 7. 明确不做（YAGNI）

- 全市场日线本地落库（由 iFinD 选股替代，且会触发频控）
- 回测引擎、策略收益率模拟
- 实盘下单 / 券商接口对接
- AI 荐股、预测涨跌
- 多用户、权限体系
- 移动端 App
- 实时分时盯盘（iFinD 高频接口不支持历史，且本地自用无必要）
- 北向资金实时净流入（2024-08 起官方已停发实时数据）

---

## 8. 实施阶段

| 阶段 | 内容 | 产出 | 状态 |
|---|---|---|---|
| P0 | 项目骨架：目录、配置、DB、ORM 模型、健康检查 | 可启动的空壳服务 | ✅ 完成 |
| P1 | `sources/`：iFinD 客户端 + Markdown 解析 + akshare 缺口源 + 限速分片；采集与回补任务 | 能跑通全量数据入库 | ✅ 完成 |
| P2 | 复盘 API + 今日复盘 Dashboard | 首页可用 | 待开始 |
| P3 | 情绪周期 + 板块 + 涨停复盘页 | 复盘主体完成 | 待开始 |
| P4 | 选股器（iFinD 自然语言 + 本地结构化） | 找票能力 | 待开始 |
| P5 | 自选股 + 个股详情 + 复盘笔记 | 全功能闭环 | 待开始 |
| P6 | 定时任务 + 数据管理页 | 自动化 | 待开始 |

每个阶段结束都应是**可运行、可验证**的状态。

### 8.1 P1 已验证结果

**当日采集**（2026-09-17，5 步全 ok）：

```
calendar    rows=8797  cost=2.87s
index       rows=5     cost=3.07s    5 个指数全部返回
limit_pool  rows=68    cost=2.52s    涨停47 / 跌停1 / 炸板20
lhb         rows=62    cost=1.76s
sentiment   rows=1     cost=0.48s
```

**情绪指标（2026-09-17）**：涨停 47 / 跌停 1 / 炸板 20 / 封板率 70.15% /
炸板率 29.85% / 最高连板 5 / 涨 2503 跌 2558 / 成交额 1.82 万亿 /
昨日涨停股今日均涨幅 +0.15%

**边界场景已验证**：
- 11 只代码分 2 片，分片前被静默丢弃的中信证券正常返回
- iFinD 429 限流经退避重试后成功
- 指数 DNS 瞬时解析失败经退避重试后成功
- akshare `push2ex` 连接超时经退避重试后成功
- 历史日期采集被护栏拦截，同时 limit_pool / lhb 正常完成
- 同一股票多条目龙虎榜（3 条不同上榜原因）按复合主键正确存储

---

## 9. 待确认事项

- **iFinD 账号权益等级未知**：免费版 2 req/s、个人版 5、企业版 10。
  建议向客服确认日调用量配额，否则定时采集可能超限。
- `sector_data` 的板块代码体系需固定（中信 / 同花顺），否则跨日不可比。
