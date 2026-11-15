# S6.12 基线审计与生产状态

- 时间：2026-11-15 17:00–17:30（Asia/Shanghai）
- 结论：基线一致；生产只读复核完成；**未写生产库、未部署、未重启**。

## 1. Git 基线

| 命令 | 结果 |
| --- | --- |
| `git branch --show-current` | `work/s6-8-s6-9-gate-closure` |
| `git rev-parse HEAD` | `1971d4993307ab7cae80c6483dea567148202813` |
| `git rev-parse @{u}` | 同上，`0 0` |
| `git diff --check` | 无输出 |
| `git status --porcelain=v1` | 仅 ` M reports/s6/s62/migration-report.json` |
| `1971d49` 是否在当前历史 | **是**（HEAD 即该提交） |

`reports/s6/s62/migration-report.json` 是 pytest 重写产物：未删除、未恢复、未提交。

## 2. G0 保护资产（before）

`reports/s6_12/protected-assets-before.json`（`scripts/s6_gate0_fingerprint.py --stage before`，exit 0）：

- `all_protected_assets_present = true`，`missing_assets = []`，`integrity_failed = []`；
- 四库 `integrity_check = ok`；
- C0 五脚本与冻结信号指纹已记录（final 见 `gate0-compare.json`）。

## 3. 生产只读复核（未做任何写入）

| 项 | 复核结果 |
| --- | --- |
| 主机 | `zhifujing.top`（腾讯云 Ubuntu 24.04.5） |
| 服务 | `fupan.service` 运行中，单 worker |
| 版本 | 无 `.git`；`main.py` SHA256 `04cb2afa…`（与 S6.11 一致） |
| 数据库 | `fupan.db` 1.83 GB + WAL 49.5 MB，WAL 模式，integrity ok |
| Schema | 49 表；`rps_daily` 缺 `ret20/ret60/rps20/rps60` |
| 备份 | 最近 11-13 23:10，integrity ok，SHA256 `c96b29b2…` |
| 依赖/配置 | `prod-config/` 下已保存 systemd、nginx、pip freeze |
| 写入 | **无**（本轮只执行 `sha256sum`、`PRAGMA` 只读查询、`tar` 读取） |

## 4. 本轮代码改动（已提交 `621e11a`）

- 新增 `backend/app/runtime_context.py` 与请求级写入边界中间件；
- 两个 `/sync` 接口改为 409；板块成分股 GET 改纯读；
- 研究缓存/计量写入在请求内跳过；
- 新增 7 项负向测试 + RPS 迁移演练脚本 + Staging 冒烟脚本。

## 5. 判定

```text
baseline_consistent = YES
unknown_changes = NONE
production_modified = NO
production_db_modified = NO
g0_before = PASS
```
