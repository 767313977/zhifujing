# S6.12 发布方案与版本指纹

- 时间：2026-11-15
- 生产版本：**`PRODUCTION_VERSION_UNIDENTIFIED`**（无 `.git`，无法映射到提交）
- 目标版本：代码提交 `621e11a` + 发布包 `fupan-2026-11-15.tar.gz`

## 1. 生产版本指纹

| 文件 | 生产 SHA256 | 11-08 包 | 11-09 包 |
| --- | --- | --- | --- |
| `backend/app/main.py` | `04cb2afa…` | `1da64c57…` ✗ | `5505eba7…` ✗ |
| `backend/app/config.py` | `3a197add…` | `bb617f75…` ✗ | `199829cf…` ✗ |
| `backend/app/jobs/scheduler.py` | `fbf0ac26…` | `fbf0ac26…` ✓ | `fbf0ac26…` ✓ |
| `backend/app/api/watchlist.py` | `fcad47dd…` | `fcad47dd…` ✓ | `fcad47dd…` ✓ |
| `backend/app/api/stock.py` | `a1399569…` | `a1399569…` ✓ | `a1399569…` ✓ |
| `backend/app/api/sector.py` | `097b4148…` | `097b4148…` ✓ | `097b4148…` ✓ |
| `backend/app/db.py` | `eab57643…` | `eab57643…` ✓ | `3a12a70c…` ✗ |
| `backend/requirements.txt` | `6725ed3c…` | `6725ed3c…` ✓ | `6725ed3c…` ✓ |
| `frontend/dist/index.html` | `9435beeb…` | `ad5f9425…` ✗ | `8c075cef…` ✗ |

结论：

- 6/9 关键文件与 **`fupan-2026-11-08.tar.gz`**（SHA256 `21cd1395…`）一致；
- `main.py`、`config.py`、`frontend/dist` 被生产侧改过（热修/重新构建），无法对应仓库提交；
- 因此标记 **`PRODUCTION_VERSION_UNIDENTIFIED`**，不擅自认定属于某个提交；
- 已保存完整只读副本 `dist-deploy/rollback-prod-20261115.tar.gz`（`main.py` 与生产逐字节一致），作为真实回滚基线。

## 2. 目标发布包

| 项 | 值 |
| --- | --- |
| 代码提交 | `a8b13a6`（含 S6.12 代码与测试修正） |
| 包文件 | `dist-deploy/fupan-2026-11-15.tar.gz` |
| 大小 | 1,913,389 字节 |
| 文件数 | 304 |
| SHA256 | `447af53308b081001c99779e911cd0e3e75da13dd3f362b0d0f9b12a5d755f55` |
| 含 | `runtime_context.py`、写入边界中间件、RPS 迁移脚本 |
| 不含 | `.env`、数据库/seed、`.venv` |

## 3. 发布顺序（待用户授权）

| 步骤 | 操作 | 状态 |
| --- | --- | --- |
| 1 | 重新核对 Git/生产状态 | 已完成 |
| 2 | 修复代码 + 测试 | 已完成（本地） |
| 3 | Staging 验证 | 已完成（隔离临时库） |
| 4 | 制作发布包 + SHA256 | 已完成 |
| 5 | 验证生产备份 | 已完成（只读） |
| 6 | 保存旧版完整副本 | 已完成（本地） |
| 7 | **用户授权生产迁移** | **PENDING** |
| 8 | 执行 RPS 迁移 | 待授权 |
| 9 | 部署新版本 | 待授权 |
| 10 | 重启 `fupan.service` | 待授权 |
| 11 | 核心功能验收 | 待授权 |
| 12 | 检查真实采集 | 待授权 |
| 13 | 确认网站正常 | 待授权 |
| 14 | 输出上线报告 | 待授权 |

## 4. 核心功能验收清单（部署后执行）

| 项 | 方法 | 期望 |
| --- | --- | --- |
| 服务运行 | `systemctl is-active fupan` | active |
| 网站可访问 | `https://zhifujing.top/api/health` | 200 |
| 登录 | 浏览器真实账号 | 成功 |
| 首页 | 打开 `/` | 有数据，无白屏 |
| 个股研究 | 打开一只已知股票 | K 线/概况有数据 |
| 板块研究 | 打开板块页 | 成分股或“当日未采集”说明 |
| 最新交易日 | `/api/market/dates` | 最新为 11-13 或部署日最近交易日 |
| 采集任务 | `journalctl -u fupan` | 调度器启动，无异常堆栈 |
| 数据库完整 | `PRAGMA integrity_check` | ok |
| 写入边界 | 打开个股页（触发自动同步） | 网络面板 409，研究表行数不变 |

⚠️ 登录验收需要真实账号；本轮**没有**生产账号凭据，标记 `NOT_VERIFIED`，部署后由用户完成或提供测试账号。

## 5. 回滚

见 `03-backup-rollback.md`：本地完整副本 + 数据库备份 + 旧列兼容。
发布失败优先恢复旧代码和服务，不删除新增列、不自动恢复数据库。
