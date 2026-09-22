# 致富经

个人自用的 **A 股每日复盘与找票站点**。收盘后自动把当天的行情、涨停、板块、资金等数据采进本地 SQLite，在浏览器里看复盘、按形态找票、记笔记。

> **这是给自己用的工具，不是产品。** 没有注册登录、没有多用户、没有权限体系，也没打算做成 SaaS。
> 仓库里**不含任何密钥、服务器地址或账号**，想跑起来得自己准备数据源凭据（见「跑起来」）。

---

## 它能干什么

| 路由 | 页面 | 内容 |
| --- | --- | --- |
| `/` | 今日复盘 | 指数卡片、成交额、涨跌家数、情绪温度、板块热力、涨停梯队、涨停明细、龙虎榜、自选股表现、复盘笔记 |
| `/sentiment` | 情绪周期 | 近 60 日涨停数 / 炸板率 / 连板高度 / 指数叠加曲线 |
| `/sectors` | 板块题材 | 板块排行与成分股、板块轮动矩阵、多板块强弱对比、**板块资金流向（同花顺口径）+ 近 N 日累计净流入曲线** |
| `/limit-up` | 涨停复盘 | 连板晋级率、涨停梯队、题材共振、首封时间分布、涨停/炸板明细（**含涨停原因**） |
| `/funds` | 资金面 | 两融走势、北向成交额、ETF 申赎排行、龙虎榜机构席位排行 |
| `/patterns` | 形态选股 | 全市场日线形态扫描（均线多头、回踩不破、N 日新高、平台突破、放量突破前高、放量上涨），命中推送到飞书 |
| `/screener` | 选股器 | **自然语言选股（iFinD，主力）** + 结构化条件 + 保存条件 |
| `/watchlist` | 自选股 | 自选池、每日表现、笔记 |
| `/stock/:code` | 个股详情 | 概况、所属题材、日/周/月 K（蜡烛 + 均线 + 成交量）、涨停记录 |
| `/settings` | 数据管理 | 采集状态与覆盖、iFinD 配额用量、手动采集 / 回补、日志 |

## 技术栈

- **后端**：Python 3.13、FastAPI、SQLAlchemy 2.0、SQLite（WAL）、APScheduler（交易日 18:00 定时采集）
- **前端**：React 19、Vite、TypeScript、Tailwind v4、ECharts 6（按需注册，见 `frontend/src/components/EChart.tsx`）
- **数据源**：iFinD 为主，akshare / 开盘红 / 同花顺数据中心补缺

## 架构：采集与页面彻底解耦

```
iFinD MCP ─┐
           ├─(限速/重试/分片/降级)─> sources ─> services(解析&计算) ─> SQLite
akshare   ─┘                                                          │
                                                         FastAPI 只读 ──> React
```

两条硬约束：

1. **页面只读 SQLite，绝不实时拉外部接口。**
   只有两处例外，且都是刻意留的：**选股器**（一次自然语言选股 = 一次 iFinD 调用，等于用户主动花配额），
   **板块成分股**（首次打开某板块时抓一次并落库，之后读库）。
2. **外部脆弱性锁死在 `backend/app/sources/` 一层。** 接口变更、限流策略调整都只改这层，不往上传染。

## 数据源与配额

| 数据 | 来源 |
| --- | --- |
| 指数、个股行情、选股、新闻公告 | iFinD MCP |
| 涨停 / 跌停 / 炸板三池、龙虎榜、交易日历 | akshare `push2ex` |
| 全市场涨跌家数 | akshare（乐咕乐股） |
| 板块分类 / 成分股 / 涨停天梯 | 开盘红（`sources/kaipanhong.py`） |
| 涨停原因、板块资金流 | 同花顺数据中心（`data.10jqka.com.cn`） |
| 指数量价、均线、背离 | 本地计算（`services/index_tech.py`，0 配额） |

**配额是这个项目最稀缺的资源**：iFinD 个人版 **5000 次 / 订阅周期**（按订阅日滚动的窗口，不是自然月），
且**账号级共享** —— 定时采集、形态选股的日线更新、手动补数全花同一个池子。
所以 `services/usage.py` 里有一套分级让路：用掉 80% 停形态日线、90% 停概念兜底、95% 只保指数 + 涨停三池 + 情绪这条主线。
「数据管理」页能直接看到本周期用量与外推值。

**明确不碰**：东财 `push2*.eastmoney.com` 集群（实测会触发本机 IP 临时频控，连接级失败），只用它的 `push2ex`。

## 目录结构

```
backend/app/
  api/        各页面用的只读接口（market / limit / sector / funds / stock / screener / watchlist / note / patterns / admin）
  jobs/       采集与回补任务（collect_*、backfill_*、scan_patterns、push_brief、scheduler）
  services/   计算层（情绪、板块、形态、指数技术指标、配额计量）
  sources/    所有外部数据源客户端，含限速 / 重试 / 分片 / 降级
  models.py   SQLAlchemy 模型；每张表的口径与坑写在 docstring 里
frontend/src/
  pages/      10 个页面
  components/ 面板与表格组件（EChart 是按需注册的 ECharts 封装）
  lib/        图表基座、格式化、排序、K 线周期等
scripts/      一次性脚本：建库 / 回补 / 探针 / 打包部署
deploy/       install.sh（systemd）、setup_nginx.sh（nginx + Basic Auth，可选 HTTPS）
docs/plans/   设计文档
```

## 跑起来

**前提**：Python 3.13+、Node 20+（开发用的是 Node 24），以及一个 iFinD 账号的 auth token。

**1）准备 `.env`**（放项目根目录，已在 `.gitignore` 里）

```ini
# 必填：iFinD MCP 的鉴权 token
IFIND_AUTH_TOKEN=

# 强烈建议填对：配额计量周期的起点日 = 你的 iFinD 订阅日。
# 填错会让配额守卫低估已用量（该让路时不动作，然后直接撞墙）。
IFIND_CYCLE_START_DAY=17

# 可选：飞书群自定义机器人的 webhook，用来推每日简报与形态命中
FEISHU_WEBHOOK_URL=

# 可选：监听地址与端口（0.0.0.0 才能用手机打开，但本服务自身没有鉴权）
HOST=127.0.0.1
PORT=8000
```

其余配置项（限速、采集时刻、形态股票池门槛、日线保留天数等）都有合理默认值，见 `backend/app/config.py`，
每一项都写了为什么是这个值。

**2）后端**

```bash
python -m venv .venv
.venv/Scripts/pip install -r backend/requirements.txt   # Windows；Linux/macOS 用 .venv/bin/pip

cd backend
../.venv/Scripts/python -m app.main                     # Windows 上也可直接双击根目录的 start.bat
```

启动后会打印本机地址与局域网地址（手机同一 WiFi 可直接打开）。

**3）前端**

```bash
cd frontend
npm install
npm run dev     # 5173，/api 已代理到 8000
```

**4）首次灌数据**

新库是空的。到「数据管理」页点手动采集，或：

```bash
curl -X POST http://127.0.0.1:8000/api/admin/collect
```

历史回补用 `scripts/backfill_*.py`，但**注意各数据源的回补边界不一样**（涨停三池只有最近 15 个交易日、
指数与龙虎榜能补约半年），详见设计文档 §4.1。形态选股的日线库首次建库要跑较长时间，且会占用大量配额，
`scheduler` 里设计了每日往前啃一小段的增量策略（`kline_backfill_days`）。

## 部署

`deploy/install.sh` 写 systemd 服务（后端绑 127.0.0.1），`deploy/setup_nginx.sh` 配 nginx + Basic Auth，
带上 `DOMAIN` 与 `CERT_EMAIL` 时会顺带签 Let's Encrypt 证书、配 80 跳转 443、装续期钩子。

**后端自身没有任何鉴权**，对外必须挡一层带密码的反向代理 —— 否则谁扫到都能看你的自选股与笔记，
还能触发采集白烧 iFinD 配额。

一键推送脚本（打包 → 上传 → 远端安装）**没有入库**：它含服务器密码，属于本机私产。
自己写的话，`scripts/pack_deploy.py` 已经把打包那步做好了（`--no-db` 出代码包，不含数据库）。

## 文档

设计文档在 [`docs/plans/2026-09-17-fupan-site-design.md`](docs/plans/2026-09-17-fupan-site-design.md)（约 4700 行）。
它不是事后补的说明书，是**边做边记的决策日志**：每个数据源怎么试出来的、哪条路走不通、
哪些数实测出来和直觉相反，都带原始观察记录。想改这个项目之前，值得先翻一遍对应章节 ——
很多看起来「绕」的实现，都是踩过坑之后才改成那样的。

## 明确不做

全市场日线本地落库（改用 iFinD 选股，且会触发频控）、回测引擎与收益率模拟、实盘下单 / 券商接口、
AI 荐股与涨跌预测、多用户与权限体系、移动端 App、实时分时盯盘、北向资金实时净流入（官方已停发）。

## 说明

- 数据版权归各数据源所有，本项目只做**本地缓存与个人研究用途**，不对外提供数据服务。
- 所有数据与结论仅供个人复盘参考，**不构成任何投资建议**。
