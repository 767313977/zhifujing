# S6.11 基线审计（只读）

- 时间：2026-11-15 16:40–16:56（Asia/Shanghai）
- 结论：**基线一致；无未知修改、无并发写入；生产主机本轮可只读访问**。
- 未执行 `reset` / `clean` / `stash` / `amend` / `force push` / 合并 / 部署。

## 1. Git 实际状态

| 命令 | 结果 |
| --- | --- |
| `git branch --show-current` | `work/s6-8-s6-9-gate-closure` |
| `git rev-parse HEAD` | `48819deda0bf581511b36c6420b60072f4e76d6f` |
| `git rev-parse @{u}` | `48819deda0bf581511b36c6420b60072f4e76d6f` |
| `git rev-list --left-right --count HEAD...@{u}` | `0 0` |
| `git diff --check` | 无输出（通过） |
| `git status --porcelain=v1` | 仅 ` M reports/s6/s62/migration-report.json` |
| 最近提交 | `48819de` docs(s6.10) / `77ec876` fix(s6.10) / `6b8a688` docs(s6.8/s6.9) |

任务书给出的历史信息核对结果：

- 报告基线 `6b8a6885`：**存在于历史**，是 S6.10 之前的提交；
- S6.10 提交 `77ec876`、`48819de`：**均存在**，HEAD 即 `48819de`。

## 2. `reports/s6/s62/migration-report.json`

| 项 | 事实 |
| --- | --- |
| 用途 | `tests/test_s6_migration*` 的 **pytest 运行产物**（迁移 dry-run/apply 的临时库路径、前后 SHA256、表摘要、`generated_at`） |
| 历史 | 仅由 `863149f feat(s6.2)` 引入一次，之后每次跑测试被重写 |
| 本次差异 | 临时目录从 `pytest-293` 变为 `pytest-19`，源/目标 SHA256、表摘要、`generated_at` 变化；**无业务含义** |
| 处理 | **未删除、未恢复、未覆盖**；保留在工作区，未纳入本轮提交 |
| 风险 | 无。它不是生产数据，也不是保护资产清单内的文件 |

## 3. 并发与环境

| 检查 | 结果 |
| --- | --- |
| 本机 Python/uvicorn 进程 | 无 |
| 本机数据库 WAL 写入 | 无（本轮未启动服务） |
| 与 upstream 差异 | `0 0` |
| 未知修改 | 无 |

## 4. G0 保护资产（before）

`reports/s6_11/protected-assets-before.json`（`scripts/s6_gate0_fingerprint.py --stage before`，exit 0）：

- `all_protected_assets_present = true`，`missing_assets = []`，`integrity_failed = []`；
- 四库 `integrity_check = ok`，size/mtime 与 S6.10 一致；
- C0 五脚本与冻结信号指纹已记录，待 final 对照。

## 5. 生产访问方式（本轮新增能力）

| 项 | 值 |
| --- | --- |
| 主机 | `<生产服务器IP>`（`zhifujing.top`，腾讯云 Ubuntu 24.04.5；IP 已脱敏） |
| 登录 | `ubuntu@` + 专用密钥 `~/.ssh/fupan_deploy`（`IdentitiesOnly`、`BatchMode`） |
| 权限 | `sudo -n true` → **NOPASSWD 可用** |
| 本轮只做 | 只读命令：`systemctl show/cat`、`ps`、`df`、`ls`、`journalctl`、`nginx -T`、`openssl x509`、`curl /api/health`、SQLite `mode=ro` 查询 |
| 未做 | 未写库、未迁移、未重启、未改配置、未启停调度、未触发采集、未读取 `.env` 值 |

## 6. `deploy/prod_probe.sh` 安全审查

审查文件：[prod_probe.sh](file:///c:/Users/76731/Documents/trae_projects/fupan/deploy/prod_probe.sh)

| 要求 | 结论 |
| --- | --- |
| 无写数据库 | 通过（脚本不访问数据库；仅 `systemctl`/`ps`/`df`/`ls`/`grep`/`journalctl`） |
| 无迁移 | 通过 |
| 无服务重启 | 通过 |
| 无配置修改 | 通过 |
| 无调度启动 | 通过 |
| 无真实采集 | 通过 |
| 不输出密码/Token/Cookie/私钥 | 通过（`systemctl cat` 只含 unit 内非敏感 `Environment`；`.env` 不在 unit 内） |
| 不覆盖生产文件 | 通过（输出到 stdout；本地保存） |

补充脚本同样只读：`systemctl show`、`/proc/PID/environ`（仅白名单键名）、SQLite `mode=ro`、
`sha256sum`、`nginx -T`、`ufw status`、`iptables -S`、`du`、`journalctl --disk-usage`。

⚠️ 执行协议：脚本经 `base64` 传输后在远端 `sudo bash` 执行，避免 CRLF 破坏；
最终综合探测退出码 **0**。

## 7. 基线判定

```text
baseline_consistent = YES
unknown_modifications = NONE
concurrent_writer = NONE
protected_assets_changed = NO
production_readonly_access = YES (ubuntu + fupan_deploy key, sudo NOPASSWD)
production_write_actions = NONE
```
