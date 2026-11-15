# S6.10 P0：发布与回滚就绪度

- 时间：2026-11-15
- 依据：`deploy/install.sh`、`deploy/setup_nginx.sh`、`deploy/s6/`、本轮 Staging 回滚证据
- **本轮不执行生产部署**；生产当前版本与目录快照未采集 → 相关项 `NOT_VERIFIED`

## 1. 版本与产物

| 项 | 值 |
| --- | --- |
| 当前生产版本 SHA | **`NOT_VERIFIED`**（未接触生产主机） |
| 目标发布版本 | 本分支 S6.10 收口提交（提交后 SHA 记录在 `push-status.md`） |
| 基线 HEAD（审计时） | `6b8a68851b35e68e1924f1705c8c083ac4db77d1` |
| 前端 dist manifest | `082da7f1bcc8ece9d9bbf102073d95a7fccf2432fe5891ac80f7e9e36a568876`（59 文件） |
| 前端构建 | `npm run build` 通过；与 S6.8 浏览器验收所用构建**同一 manifest** |
| 后端依赖 | `backend/requirements.txt` 未改 |
| 前端依赖 | `package.json` / `package-lock.json` 未改 |
| 发布包 SHA256 | 需在真正打包时用 `scripts/pack_deploy.py` 生成并记录（本轮未打包） |

## 2. 配置差异（相对上一版）

| 文件 | 差异 | 影响 |
| --- | --- | --- |
| `deploy/install.sh` | systemd 新增 `Environment=AUCTION_COLLECT_ENABLED=false` | 生产 9:15–9:26 不注册竞价采集 |
| `backend/app/config.py` | 新增 `auction_collect_enabled=False` | 默认关闭，配置可覆盖 |
| `backend/app/jobs/scheduler.py` | 仅当开关为 true 才 add_job | 默认不跑竞价 |
| `deploy/s6/egress-policy.json` | loopback `allow_any_port:false`、`ports:[5000]` | 应用层出站收紧 |
| `backend/app/external_guard.py` | 回环端口策略 + 公网 IP 断言 | 出站收紧 |
| `backend/app/services/stock_phase.py` | 拆分只读模式改用 `research_session_scope` | 修复 `/api/stock/{code}` 500 |

**无新增环境变量是上线必填项**（竞价开关默认值即安全值）。

## 3. 数据库迁移差异

- 本版本**没有新的 Schema 迁移**；
- G7 迁移复核（`reports/s6-followup/g7/migration-revalidation.json`，本轮 15:44）：
  `status=PASS`、`idempotent=true`、`recovered=true`、源库 SHA256 不变；
- 因此**不存在“不可逆迁移”**，不构成发布阻断项；
- 仍然禁止发布脚本自动迁移：`schema_mode=verify`，缺表即启动失败。

## 4. 发布步骤（人工执行，非自动）

```bash
# 0) 前置：记录当前版本、备份、确认回滚包可用
cd /opt/fupan && git rev-parse HEAD 2>/dev/null || true   # 或记录部署包版本文件
sudo -u fupan cp -p backend/data/fupan.db backend/data/fupan.db-pre-<版本>
# 备份一份上一版代码目录（不覆盖）
sudo cp -a /opt/fupan /opt/fupan-prev-<时间戳>

# 1) 上传并解压新包到 /opt/fupan（部署包不含 .env 与 fupan.db）
# 2) 安装/更新
sudo bash deploy/install.sh          # set -euo pipefail；失败即停

# 3) install.sh 内置自检
#    - systemctl restart fupan
#    - scripts/check_dist.py 从首页递归请求所有 chunk/CSS/字体，缺一即失败退出
```

`install.sh` 的失败语义：

- Python < 3.12、缺 `.env`、前端产物自检失败 → **非零退出**，不会“带病启动”；
- 已有 `fupan.db` 时，包内 `fupan.db.seed` **不会**自动覆盖，除非显式 `--force-db`
  （显式覆盖前会先 `cp -p` 备份，并清理旧 WAL/SHM）；
- 未启用任何自动迁移。

## 5. 启动后健康检查

| 检查 | 命令 | 期望 |
| --- | --- | --- |
| 进程存活 | `systemctl is-active fupan` | `active` |
| liveness | `curl -fsS http://127.0.0.1:8000/api/health` | 200 |
| 前端产物完整 | `python scripts/check_dist.py --base http://127.0.0.1:8000` | exit 0 |
| 登录 | 浏览器 `https://<域名>/login` | 可登录 |
| readiness | `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/ready` | ⚠️ 见下 |

⚠️ **`/api/ready` 在当前构建下必然 503**：`EGRESS_OS_POLICY_VERIFIED` 恒为
`NOT_VERIFIED`（`readiness.py` 刻意 fail-closed），且 Staging 门禁要求
`EXTERNAL_FETCH_DISABLED`，与生产相反。**生产监控不得以 `/api/ready` 作为健康判据**，
只能用它做“安全门禁未闭环”的审计信号。这也是上线前必须明确接受的项。

## 6. 回滚

### 6.1 回滚命令（代码 / 静态产物）

```bash
sudo systemctl stop fupan
# 切回上一版目录（发布前已 cp -a 的快照）
sudo rsync -a --delete /opt/fupan-prev-<时间戳>/ /opt/fupan/
sudo systemctl restart fupan
curl -fsS http://127.0.0.1:8000/api/health
```

**不会自动恢复数据库**。只有确认新版本已写入不兼容数据、且已备份时才人工恢复
（步骤见 `backup-restore-readiness.md` §3）。

### 6.2 已验证的回滚能力（Staging）

`reports/s6_8_9/staging/versioned-rollback-report.md`：

- OLD → NEW → OLD → NEW 四次真实切换，`serving entry SHA` 每次都变；
- 每步 `health=200`、登录 smoke 通过；
- 旧版已知白屏可在版本对照中复现，说明是**真实版本切换**而非缓存；
- 前端 manifest：OLD `b74306ce…` / NEW `8eb65c3c…`。

限制：单端口顺序切换，不是生产发布系统 / 负载均衡级回滚。

### 6.3 回滚失败处理

1. 保持服务停止，不在异常状态继续写入；
2. 用 `fupan.db-before-restore-*` 与最近备份核对数据状态；
3. 若旧版代码也起不来：只启动 `--host 127.0.0.1` 的临时实例做只读诊断，
   不对外放流量；
4. 记录时间线、版本 SHA、错误日志，交由回滚决策人（`PENDING`）决定；
5. 不允许“先删库再重建”作为回滚手段。

## 7. 发布阻断项

| 项 | 是否阻断 | 说明 |
| --- | --- | --- |
| 不可逆数据库迁移 | **否** | 本版本无迁移 |
| 生产当前版本 SHA 未记录 | **是** | 无法证明可回滚到“上一版” |
| 生产目录快照/回滚包未准备 | **是** | 发布前必须 `cp -a` |
| `/api/ready` 恒 503 | **是（需明确接受）** | 监控不能用它判健康 |
| 竞价真实采集 | 已默认关闭 | 不阻断 |
| 冻结资产/C0 | 未改动 | 不阻断 |

## 8. 结论

```text
deployment_steps_documented = YES
deployment_auto_migration = NO
deployment_auto_db_overwrite = NO
deployment_auto_auction_collect = NO
rollback_proven_on_staging = YES
rollback_proven_on_production = NO (NOT_VERIFIED)
target_sha_recorded = AFTER_COMMIT (见 push-status.md)
blocking_for_go_live = YES
```
