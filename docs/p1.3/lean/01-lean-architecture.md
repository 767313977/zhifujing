# 轻量化架构方案

> 适用范围：致富经（个人自用 + 少量朋友）
> 目标：**不推倒重写**，保留现有单体与 SQLite，把工程投入收敛到「稳定性、数据安全、可维护」三件事。
> 本文只做设计与决策，不改代码、不部署、不接触生产库。

---

## 1. 定位与结论

| 维度 | 结论 |
| --- | --- |
| 用户规模 | 1 人主用，未来个位数朋友；无并发运营诉求 |
| 部署形态 | **单机 + 单进程 + 单 SQLite 文件**（现状已满足） |
| 变更方式 | 覆盖式发版 + systemd 重启，保留上一版代码包与库备份 |
| 明确不做 | 微服务、PostgreSQL、Redis、消息队列、分布式锁、K8s、多活 |

一句话：**现有架构已经是轻量架构，本轮不做重构，只做「减法 + 门禁收敛」。**

---

## 2. 现状基线（已核实）

| 项 | 事实 | 来源 |
| --- | --- | --- |
| 后端 | FastAPI + SQLAlchemy 2.0 + Python 3.13 | `backend/app/main.py`、`requirements.txt` |
| 存储 | SQLite，WAL 模式，单库 `backend/data/fupan.db` | `backend/app/db.py` |
| 调度 | APScheduler `BackgroundScheduler`，与 Web 同进程 | `backend/app/jobs/scheduler.py` |
| 采集时刻 | 15:05 主采集；17:30 / 19:30 收盘后补采；22:00 / 22:30 成分股与终局收尾 | `backend/app/config.py` |
| 进程模型 | systemd 单服务，`uvicorn --host 127.0.0.1 --port 8000`，**未指定 --workers（即 1 个）** | `deploy/install.sh` |
| 采集互斥 | `threading.Lock`（`collect_guard`），进程内串行 | `backend/app/jobs/collect_daily.py` |
| 研究缓存 | 进程内 LRU：TTL 600s / 16 条 / single-flight / 并发闸门 2 | `backend/app/services/research_screen.py` |
| 数据版本 | `data_revision` 表 + 8 域 × 3 动作 = **24 条触发器** | `backend/app/services/data_revision.py` |
| 备份 | 在线备份 API，保留 5 份 | `backend/app/jobs/backup.py` |
| 迁移 | `create_all` + 补列 + 补索引，**幂等**；主库需显式 `--allow-main-db` | `backend/app/db.py`、`scripts/migrate_db.py` |
| 启动门禁 | `schema_mode=verify`：缺表/缺列/缺索引直接 `RuntimeError`，**fail-closed** | `backend/app/db.py` |

数据体量（区分本机代理库与真实生产库）：
- **本机代理库** `prod_replica.db`：491,520 B，49 表、0 行，仅用于演练；
- **真实生产库** `fupan.db`：约 **1.74 GB**（生产实测值，非本机估值），含 158,235 行 RPS、2 条 `trade_journal`；
- RPS 生产基线 31 个交易日 / 158,235 行。

Python 兼容性：生产服务器为 **Python 3.12.3**，本机开发为 3.13.13。已核实代码无 3.13 专属语法（无 `type` 语句、无 `match/case`、无 `tomllib` 依赖），`pandas==3.0.5` 等依赖均支持 ≥3.11。**上线前必须在生产 3.12.3 上实际跑通 `migrate_db.py --status` 与 `install_data_revision.py --status`**，不得用本机 3.13 结果代替。

---

## 3. 目标架构（与现状一致，不做改动）

```
┌────────────────────── 2 核 / 4 GB 单机（Ubuntu） ──────────────────────┐
│  systemd: fupan.service（唯一进程）                                    │
│    └─ uvicorn app.main:app  --host 127.0.0.1 --port 8000  --workers 1   │
│         ├─ FastAPI 只读查询 ──────────────► SQLite (WAL, 单文件)        │
│         ├─ APScheduler 交易日定时采集 ────► SQLite（写）                │
│         └─ 进程内研究缓存（LRU / TTL / 可失效）                          │
│  nginx：仅做反代与超时（可选）                                           │
│  /opt/fupan/backend/data/fupan.db  +  -wal / -shm                      │
│  /opt/fupan/backend/data/fupan.db-bak-*（保留 5 份）                    │
└────────────────────────────────────────────────────────────────────────┘
```

**关键约束（必须保持，不得「优化」掉）**

1. **单进程**。`collect_guard` 是 `threading.Lock`，只在单进程内有效；一旦 `--workers > 1`，采集互斥失效、APScheduler 会被每个 worker 各起一份，直接产生重复采集与重复推送。
2. **SQLite 单写者**。不引入第二个写进程；备份走在线备份 API（`Connection.backup`），不能用 `cp` 拷 WAL 库。
3. **页面只读库**。除已存在的少数「按需抓一次并落库」例外（板块成分、公告/研报/解禁、新闻、DDE、杀猪盘体检），不新增实时外部调用。
4. **登录与用户数据隔离保留**。这是现有功能，不是待简化项。

---

## 4. 资源预算（2 核 / 4 GB，生产实测口径）

**口径声明**：下列数值为**生产服务器实测或可测量值**，采集命令为 `systemctl show fupan -p MemoryCurrent,MemoryPeak`。不用开发环境估值代替生产实测；若某项尚无实测值，标注「待生产实测」并给出保守上限。

| 场景 | 预算上限 | 测量方式 | 处置 |
| --- | --- | --- | --- |
| Web 常驻空闲 | 待生产实测（保守 ≤ 600 MB）；历史实测主进程 RSS ≈ 1006 MiB | `systemctl show fupan -p MemoryCurrent`（服务稳定 10 分钟后） | 不干预 |
| 盘后采集一趟 | MemoryPeak ≤ 3.2 GB | `systemctl show fupan -p MemoryPeak`（采集结束后立即读） | 超限按《05》回滚 |
| 研究全市场冷扫描 | MemoryPeak ≤ 3.0 GB | 同上，触发一次研究筛选后读 | 保留 `_LOW_START_CONCURRENCY=2` |
| 定时备份 | +≤ 200 MB（在线备份按页） | 同采集 | 保留 |
| 前端构建 | 不发生在服务器 | 本地 `pack_deploy.py` 打包 | 保留 |

**历史实测基线（非当前采样，2026-11-10 补录）**：此前生产运行状态实测记录：

| 指标 | 实测值 |
| --- | --- |
| `MemoryCurrent` | 1740931072 B（≈ 1.62 GiB） |
| `MemoryPeak` | 2120224768 B（≈ 1.98 GiB） |
| 主进程 RSS | ≈ 1006 MiB |

这组数值**是历史实测，不是本轮采样**：它只能说明「当时的运行状态在红线以内」，不能替代发布前的运行状态检查。正式发布前仍须按 G6 重新做一次只读检查（`systemctl show fupan -p MemoryCurrent,MemoryPeak` + RSS）。


**内存红线**：`MemoryPeak > 3.2 GB` 或出现 `OOMKill`，按《05》回滚流程处理，不在线上边跑边调参。
**不做**：不引入内存缓存服务、不做多进程、不做预计算常驻大对象。

---

## 5. 数据安全边界（三主体，简化表述）

| 主体 | 允许 | 禁止 |
| --- | --- | --- |
| 生产服务器 | 运行服务、定时采集、本地备份、只读探针 | 把库文件上传网盘/仓库；把明文贴进聊天 |
| 可信本机（隔离目录） | 演练、哈希、迁移预演 | 把含真实用户数据的库交给云端模型 |
| TRAE（云端模型） | 读代码、读脱敏证据、写方案 | 读数据库业务明文；执行生产写操作 |

落实方式：**文件不出本机、明文不进对话、命令逐条带执行主体**。不新增加密体系、不新增密钥管理服务。

---

## 6. 本方案的维护成本

| 措施 | 新增维护成本 | 收益 |
| --- | --- | --- |
| 保持单进程单库 | 0 | 不需要处理分布式一致性 |
| 保留 24 触发器 | 0（已存在） | 缓存失效有确定依据 |
| 保留进程内缓存 | 0（已存在） | 重启即恢复，无外部依赖 |
| 保留在线备份 | 每天 1 次、保留 5 份 | 出事可回退到上一交易日 |
| 上线门禁 7 条 | 每次发版约 1 小时 | 把「白屏/丢数据/重复采集」变成显式失败 |

**总原则：新增任何组件前，先回答「它解决哪个已经发生过的故障」。答不出就不加。**
