# S6 Gate 0 运行环境清单（runtime-inventory）

- 生成时间：2026-11-15T00:05+08:00
- 分支：`release/s6-canary-hardening`（自 `104dfdf` 创建，`3060e82` 是其祖先）
- 采集主机：`HAN-LAPTOP`（Windows 11 开发机，非生产）
- 结论适用范围：**只有开发机 + 仓库代码可核实；生产主机与生产网络不可访问**。
  凡标 `NOT_VERIFIED` 的条目不得当作已通过。

## 1. 计算与运行时

| 项 | 实测值 | 来源 |
| --- | --- | --- |
| 开发机 OS | Windows-11-10.0.26340-SP0 | `platform.platform()` |
| 开发机 Python | 3.13.13（`.venv`） | `python --version` |
| Node / npm | v24.19.0 / 11.17.0 | `node --version` / `npm --version` |
| 后端依赖锁 | `backend/requirements.txt`（SHA256 见 `dependency-inventory.txt`） | 仓库文件 |
| 前端依赖锁 | `frontend/package-lock.json`（SHA256 见 `dependency-inventory.txt`） | 仓库文件 |
| 生产目标 OS | Ubuntu 24.04+ / Debian 13+（`deploy/install.sh` 第 28–40 行要求 Python ≥3.12） | `deploy/install.sh` |
| 生产目标 Python | 3.12+（install.sh 默认 `python3`，可覆盖为 `python3.12`） | `deploy/install.sh` |
| 容器 / 虚拟机 / 物理机 | **`NOT_VERIFIED`**：本轮无法接触生产主机 | — |
| 生产服务账号 | systemd `User=fupan`、`Group=fupan`（非 root） | `deploy/install.sh` unit 段 |
| 当前开发机 shell 权限 | `han-laptop\76731`，管理员=True | `whoami` / `IsInRole(Administrator)` |

关键网络库（`pip list` 实测）：`requests 2.34.2`、`urllib3 2.8.0`、`websockets 17.1`、
`websocket-client`（`websocket` 模块）、`tushare 1.4.29`、`akshare 1.18.96`。
`httpx`、`aiohttp` **未安装**（`importlib.util.find_spec` 返回缺失）——因此 A0 盘点中
这两条为「当前无依赖，但 wrapper 仍需覆盖未来引入」。

## 2. 服务进程模型

| 项 | 实测 / 代码事实 |
| --- | --- |
| 启动命令（生产） | `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`（`deploy/install.sh` unit `ExecStart`） |
| worker 数量 | **1**（命令未带 `--workers`，uvicorn 默认单 worker；代码注释亦要求单 worker） |
| 进程模型 | 单进程：Web + APScheduler 调度器**同进程**（`main.lifespan()` 内 `start_scheduler()`） |
| 调度器开关 | `SCHEDULER_ENABLED`（生产 systemd 固定 `true`，见 `deploy/install.sh` 第 150 行） |
| 外部取数开关 | `EXTERNAL_DATA_FETCH_ENABLED`（默认 true；`external_guard.py` 内可进程级锁定为 false） |
| Schema 启动模式 | 生产默认 `verify`（只检查不改库）；开发 `python -m app.main` 为 `migrate` |
| 文档路由 | `/docs`、`/redoc`、`/openapi.json` 已关闭（`main.py` FastAPI 参数） |

## 3. 数据库

| 库 | 路径 | 大小（字节） | mtime（本机） | 本轮用途 |
| --- | --- | --- | --- | --- |
| `fupan.db` | `backend/data/fupan.db` | 513,519,616 | 2026-10-31 22:26:44 | 站点主库，**本轮只读** |
| `history.db` | `backend/data/history.db` | 869,801,984 | 2026-10-20 14:14:38 | 历史行情，**本轮只读** |
| `b1_research.db` | `backend/data/b1_research.db` | 1,441,378,304 | 2026-11-09 12:23:12 | 研究库，**本轮只读** |
| `forward_research.db` | `backend/data/forward_research.db` | 67,055,616 | 2026-11-11 17:17:58 | 前瞻研究库，**本轮只读** |

- 连接方式：SQLAlchemy + SQLite（`sqlite:///` 本地文件）。
- 生产路径：`APP_DIR=/opt/fupan`（可覆盖），即 `/opt/fupan/backend/data/*.db`。
- 生产读写权限：**`NOT_VERIFIED`**（无生产主机访问权）。
- 本轮所有写入类测试只允许在**临时副本**上进行（见 `reports/s6/runtime/database-isolation-report.md`）。

## 4. 部署与入口

| 项 | 事实 |
| --- | --- |
| 部署目录 | `/opt/fupan`（`deploy/install.sh` `APP_DIR`） |
| 服务管理器 | systemd，unit `fupan.service`，`Restart=always`、`RestartSec=10`、`TimeoutStopSec=30` |
| 反向代理 | nginx（`deploy/setup_nginx.sh`），`proxy_pass http://127.0.0.1:8000` |
| TLS 终止 | nginx（可选 ACME 证书；后端只监听回环明文） |
| 对外端口 | 默认 8080；配置 `DOMAIN` 后 80/443 + HTTPS |
| nginx Basic Auth | 2026-09-28 起默认 `AUTH=off`（后端自带登录体系） |
| CORS | 生产 `ENABLE_DEV_CORS=false`（systemd 环境变量） |

## 5. 网络拓扑与出口

| 项 | 事实 |
| --- | --- |
| 开发机代理环境变量 | `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY` 均为空 |
| 生产出口代理 | **`NOT_VERIFIED`**：仓库内无 egress proxy 配置，生产主机无访问权 |
| 生产 DNS 解析路径 | **`NOT_VERIFIED`**（`dns_guard=NOT_INTERCEPTED` 仍未关闭） |
| 生产防火墙 / 网络命名空间 | **`NOT_VERIFIED`**：本轮只能给脚本与方案，未在生产或隔离 OS 环境执行 |
| IPv4 / IPv6 出站 | 应用层 wrapper 覆盖两者（见 `security/egress-inventory.md`）；OS 层 `NOT_VERIFIED` |

## 6. 本轮环境限制（必须随报告披露）

1. 本轮**没有**可用的隔离 OS / 网络命名空间 / 容器环境来执行真实防火墙与 DNS 拦截测试；
   因此 G3（OS 级默认拒绝）与 G4（DNS/子进程/原生库路径）不能判定为通过。
2. 生产主机、生产数据库、生产网络策略均不可访问；任何「生产已生效」的表述都不成立。
3. Windows 开发机上安装了全局防火墙规则会污染用户日常环境，任务书 §A2 明确禁止；
   本轮只输出脚本与验证方案，不执行。
