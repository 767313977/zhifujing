# S6-B2 启动锁定与 readiness

## 1. 启动顺序（代码事实 + 测试证明）

`backend/app/main.py::lifespan` 的顺序：

1. `ensure_schema()`（生产默认 **verify**，缺表/缺列直接启动失败，不改库）；
2. `readiness.record_startup(schema_result)`（只记录，不写库）；
3. `lock_offline_mode()`：首次读到 `EXTERNAL_DATA_FETCH_ENABLED=false` 即锁定；
4. `install_network_deny()`：安装 requests / socket / TLS / DNS / 代理 wrapper
   （**在线也安装**，避免运行时切换出现窗口）；
5. 打印出站策略版本与条数；
6. `start_scheduler()`；
7. 可选：后台线程预热形态缓存。

测试 `test_startup_order_and_readiness_endpoint` 在 `start_scheduler` 处插入探针，
断言那一刻 `installed=True`、`dns_guarded=True` —— 即**业务模块开始运行前**
安全控制已经安装（不是靠 import 顺序的偶然性）。

## 2. liveness 与 readiness

| 端点 | 含义 | 本轮实际 |
| --- | --- | --- |
| `GET /api/health` | 进程存活；免登录；`detail=1` 仅内网返回三个 bool | 200 `{"status":"ok"}` |
| `GET /api/ready` | 灰度必要门禁全部 PASS 才 200 | **503**（OS 门禁 NOT_VERIFIED） |

`/api/ready` 响应只含门禁名、状态、稳定原因码与策略条数，不含路径 / token / 主机名。

## 3. 门禁状态（本轮实测）

| 门禁 | 状态 | 原因 |
| --- | --- | --- |
| `STARTUP_CONFIG_VALID` | PASS | schema_mode=verify；开发机无生产冲突项 |
| `EGRESS_APP_POLICY_LOCKED` | PASS | wrapper 已安装；策略 17 条 loaded |
| `EGRESS_OS_POLICY_VERIFIED` | **NOT_VERIFIED** | 无隔离 OS/网络环境；固定值，不可用环境变量伪造 |
| `DATABASE_READ_ONLY_VERIFIED` | **FAIL** | 引擎是 `sqlite:///path`（读写）；探针证明 `mode=ro` 能拒绝写，但应用没用它 |
| `SCHEDULER_DISABLED` | PASS | 测试/灰度环境 `SCHEDULER_ENABLED=false`，调度器未运行 |
| `EXTERNAL_FETCH_DISABLED` | PASS | 离线锁定 |
| `CANARY_READINESS` | **FAIL** | 存在 blocking 项 |

## 4. 为什么 readiness 不能「变绿」

- `EGRESS_OS_POLICY_VERIFIED` **不读环境变量**、不读配置文件，固定 `NOT_VERIFIED`；
  只有将来把真实 OS 验证结果接入（例如读取 `verify-egress.sh` 的签名证据）才可能 PASS。
- `DATABASE_READ_ONLY_VERIFIED` 检查的是**应用实际引擎 URL** 是否含 `mode=ro`，
  不是「文件能否被只读打开」；手工改环境变量无法让它通过。
- `SCHEDULER_DISABLED` 检查的是调度器实例是否真的在运行，不是只看配置项。
- 每次请求重新计算，不缓存启动时的结论。

## 5. 结论

- G6（启动锁定与 readiness）在应用层 **PASS**。
- G7（数据库只读）**FAIL** → 灰度阻塞。
- G3（OS 出站）**NOT_VERIFIED** → 灰度阻塞。
