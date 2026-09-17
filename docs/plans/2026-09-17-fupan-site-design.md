# 每日复盘与选股网站 — 设计文档

- 日期：2026-09-17
- 定位：**个人自用**、本地运行、**A 股**为主
- 技术方案：FastAPI + SQLite + React（方案 ①）

---

## 1. 环境与可行性验证结论

已验证环境：

| 项 | 版本 | 结论 |
|---|---|---|
| Python | 3.13.13 | 可用 |
| Node / npm | 24.19.0 / 11.17.0 | 可用 |
| git | 2.55.0 | 可用 |
| akshare | 1.18.96 | 安装成功，Python 3.13 兼容 |
| pandas | 3.0.5 | 可用 |
| fastapi | 0.141.1 | 可用 |

### 1.1 数据源实测（重要）

用 [probe_akshare.py](../../scripts/probe_akshare.py) 实测 11 个接口：

**可用：**

- `tool_trade_date_hist_sina` 交易日历
- `stock_zt_pool_em` 涨停池（89 条）
- `stock_zt_pool_dtgc_em` 跌停池（4 条）
- `stock_zt_pool_zbgc_em` 炸板池（11 条）
- `stock_lhb_detail_em` 龙虎榜（73 条）

**失败：** `stock_zh_a_spot_em`（全 A 快照）、`stock_board_industry_name_em`（行业板块）、
`stock_board_concept_name_em`（概念板块）、`stock_individual_fund_flow_rank`（个股资金流）、
`stock_sector_fund_flow_rank`（板块资金流）、`stock_zh_a_hist`（个股日线）。

### 1.2 失败原因定位

排查过程：

1. 裸调 `82.push2.eastmoney.com/api/qt/clist/get`（带浏览器 UA）→ **200，数据正常**
2. 拦截 akshare 实际请求 → 它请求的是**同一个 URL**，但 `headers=None`
3. 补 UA 后重试 → 仍失败；等待 75s 后重试 → 仍失败
4. 多主机对比 → `82.push2` / `push2` / `17.push2` / `push2his` **全部连接级失败**，
   而 `push2ex` 返回 404（TCP/TLS 正常，仅路径错）

**结论：**

- 失败**不是 UA 问题、不是接口下线**，而是 `push2*.eastmoney.com` 集群对本机 IP 的
  **临时频控**（由密集请求触发，会自行恢复）。
- `push2ex.eastmoney.com`（涨停池 / 龙虎榜）是**独立集群**，不受影响。
- 触发原因：`stock_zh_a_spot_em` 拉全市场约 5400 只，akshare 内部按 `pz=100` 分页，
  **单次调用即产生 ~54 个请求**，必然触发限流。

### 1.3 直接影响的架构决策

1. **数据采集与页面访问必须解耦**：页面只读 SQLite，绝不实时拉外部接口。
2. **必须限速**：全局令牌桶，东财 ≤1 req/s；失败指数退避。
3. **全市场快照主源改用新浪 / 腾讯**——两者都支持**批量**（一次请求返回多只股票），
   实测可用，抗限流能力远强于东财分页。这是本设计最关键的一处调整。

### 1.4 实测可用的兜底数据源

| 源 | 地址 | 实测 | 特点 |
|---|---|---|---|
| 新浪实时 | `hq.sinajs.cn/list=sh600000,sz000001` | 200，539 字节 | 支持批量，单次可请求数百只 |
| 腾讯实时 | `qt.gtimg.cn/q=sh600000,sz000001` | 200，1018 字节 | 支持批量 |
| 新浪全市场 | `vip.stock.finance.sina.com.cn/.../Market_Center.getHQNodeData` | 200，含北交所 | 可翻页取全 A |

---

## 2. 架构

```
fupan/
├── scripts/probe_akshare.py      # 数据源健康探测（已完成）
├── backend/
│   ├── app/
│   │   ├── main.py               # FastAPI 入口 + 静态托管前端产物
│   │   ├── config.py             # 配置（限速、路径、数据源开关）
│   │   ├── db.py                 # SQLAlchemy engine / session
│   │   ├── models.py             # ORM 模型
│   │   ├── schemas.py            # Pydantic 响应模型
│   │   ├── api/                  # 路由层
│   │   │   ├── market.py         # 大盘 / 情绪
│   │   │   ├── limit.py          # 涨停 / 跌停 / 炸板
│   │   │   ├── sector.py         # 板块题材
│   │   │   ├── stock.py          # 个股详情
│   │   │   ├── screener.py       # 选股器
│   │   │   ├── watchlist.py      # 自选股
│   │   │   ├── note.py           # 复盘笔记
│   │   │   └── admin.py          # 数据管理
│   │   ├── sources/              # 数据源适配层（隔离 akshare 脆弱性）
│   │   │   ├── base.py           # 限速器 / 重试 / 统一 UA / 多源降级
│   │   │   ├── eastmoney.py      # 涨停池、龙虎榜、板块
│   │   │   ├── sina.py           # 全市场快照（主源）
│   │   │   └── tencent.py        # 全市场快照（兜底）
│   │   ├── services/             # 业务计算（情绪指标、选股条件编译）
│   │   └── jobs/
│   │       ├── collect_daily.py  # 收盘后采集
│   │       ├── backfill.py       # 缺数回补
│   │       └── scheduler.py      # APScheduler
│   ├── data/fupan.db             # SQLite
│   └── requirements.txt
└── frontend/                     # React + Vite + TS + Tailwind + ECharts
```

**数据流：**

```
外部数据源 ──(限速/重试/降级)──> sources ──> services(清洗&计算) ──> SQLite
                                                                      │
                                                          FastAPI 只读 ──> React
```

关键点：**外部接口的脆弱性被限制在 `sources/` 一层**。akshare 哪天又挂了，只改这一层。

---

## 3. 数据模型（SQLite）

| 表 | 主键 | 说明 |
|---|---|---|
| `trade_calendar` | trade_date | 交易日历 |
| `index_daily` | trade_date, code | 指数日线（上证/深成/创业板/科创50/中证2000/北证50） |
| `stock_basic` | code | 名称、市场、板块、上市日、总市值、流通市值、行业 |
| `stock_daily` | trade_date, code | 日线：OHLC、涨跌幅、成交量额、振幅、换手率、量比 |
| `limit_pool` | trade_date, code, pool_type | 涨停/跌停/炸板：封板资金、首次封板时间、开板次数、连板数 |
| `sector_daily` | trade_date, sector_code | 板块行情：涨跌幅、成交额、上涨家数、领涨股 |
| `sector_member` | sector_code, stock_code | 板块成分股 |
| `money_flow` | trade_date, code | 主力/超大单/大单/中单/小单净流入 |
| `lhb` | trade_date, code, reason | 龙虎榜：净买额、买入额、卖出额、上榜原因 |
| `market_sentiment` | trade_date | **物化**情绪快照，见下 |
| `watchlist` | code | 自选股 + 备注 + 标签 |
| `review_note` | trade_date | 复盘笔记：市场观点、次日计划 |
| `screen_preset` | id | 保存的选股条件（JSON） |
| `collect_log` | id | 采集日志：任务、状态、耗时、错误 |

**`market_sentiment` 物化字段**（情绪曲线要快速读 60 天，实时计算太慢）：

`limit_up_count` 涨停数、`limit_down_count` 跌停数、`broken_count` 炸板数、
`seal_rate` 封板率、`broken_rate` 炸板率、`max_consecutive` 最高连板、
`up_count`/`down_count` 涨跌家数、`total_amount` 成交额、
`yesterday_limit_today_avg` 昨日涨停股今日均涨幅（打板赚钱效应）。

---

## 4. 采集流程

**触发时机：** 交易日 15:05 后（收盘数据已稳定）+ 支持手动补数。

顺序（每步独立事务、独立日志、失败不阻塞后续）：

1. 交易日历
2. 指数日线（6 个核心指数）
3. **全市场快照 → `stock_daily`**（新浪批量为主源，腾讯兜底；东财仅作最后选项）
4. 涨停池 / 跌停池 / 炸板池（东财 `push2ex`，最稳）
5. 板块行情 + 板块成分
6. 龙虎榜
7. 计算并写入 `market_sentiment`

**限速策略：**

- 全局令牌桶：东财 1 req/s，新浪 2 req/s，腾讯 2 req/s
- 失败退避：2s → 4s → 8s，最多 3 次
- 每个任务独立超时，超时记为 `partial` 而非 `failed`
- 所有请求统一携带浏览器 UA + Referer

---

## 5. API

```
GET  /api/market/overview?date=          大盘卡片 + 情绪指标
GET  /api/market/sentiment?days=60      情绪周期曲线
GET  /api/limit/pool?date=&type=up|down|broken   涨停梯队
GET  /api/sector/rank?date=&sort=        板块排序（涨停数/涨幅/资金）
GET  /api/sector/{code}/members?date=    板块成分股
GET  /api/stock/{code}/daily?days=120    K 线
GET  /api/stock/{code}/profile           个股概况
POST /api/screener/run                   执行选股
GET  /api/screener/fields                可选字段元数据
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
| `/screener` | 选股器 | 多条件组合筛选 + 保存条件 |
| `/watchlist` | 自选股 | 自选池、每日表现、笔记 |
| `/stock/:code` | 个股详情 | K 线 + 均线 + 量能、资金流、所属板块、龙虎榜 |
| `/settings` | 数据管理 | 采集状态、手动补数、日志 |

**选股器条件维度**（覆盖常见复盘找票需求）：
涨跌幅、量比、换手率、成交额、流通市值、均线位置（5/10/20/60 上下方）、
N 日新高/新低、连板数、是否涨停/炸板、所属板块、主力净流入。

---

## 7. 明确不做（YAGNI）

- 回测引擎、策略收益率模拟
- 实盘下单 / 券商接口对接
- AI 荐股、预测涨跌
- 多用户、权限体系
- 移动端 App
- 实时分时盯盘（数据源限流下不可靠，且本地自用无必要）

---

## 8. 实施阶段

| 阶段 | 内容 | 产出 |
|---|---|---|
| P0 | 项目骨架：目录、配置、DB、ORM 模型、健康检查 | 可启动的空壳服务 |
| P1 | 数据层：`sources/` 限速重试多源降级 + 采集任务 + 回补 | 能跑通全量数据入库 |
| P2 | 复盘 API + 今日复盘 Dashboard | 首页可用 |
| P3 | 情绪周期 + 板块 + 涨停复盘页 | 复盘主体完成 |
| P4 | 选股器（条件编译 + 前端） | 找票能力 |
| P5 | 自选股 + 个股详情 + 复盘笔记 | 全功能闭环 |
| P6 | 定时任务 + 数据管理页 | 自动化 |

每个阶段结束都应是**可运行、可验证**的状态。

---

## 9. 已知风险

| 风险 | 影响 | 对策 |
|---|---|---|
| 东财 IP 频控 | 快照/板块拉取失败 | 新浪腾讯批量兜底；限速；退避重试 |
| akshare 接口变更 | 采集中断 | 隔离在 `sources/`；保留 `probe_akshare.py` 定期自检 |
| 新浪/腾讯无官方 SLA | 数据延迟或字段变动 | 多源交叉校验（如成交额比对） |
| 首次回补数据量大 | 初启动耗时长 | 分批回补 + 断点续传 + 进度可见 |
| 北向资金实时数据自 2024-08 起停止披露 | 无法做"北向实时净流入" | 只做沪深股通成交额 + 季度持仓 |
