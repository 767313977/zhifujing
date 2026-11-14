# Gate 0：Git 基线与保护资产复核（S6 后续攻坚）

- 采集时间：2026-11-15T02:04:24+08:00
- 仓库根：`C:/Users/76731/Documents/trae_projects/fupan`
- 基线分支：`release/s6-canary-hardening-v2` @ `5db5246010c884a8a980e8279e4b49cb164a3603`
- 本轮工作分支：`release/s6-canary-hardening-v3`（从上述 clean HEAD 新建）
- Gate 0 结论：**PASS**

## 1. 实测命令与结果

| 命令 | 结果 |
| --- | --- |
| `git rev-parse --show-toplevel` | `C:/Users/76731/Documents/trae_projects/fupan` |
| `git status --short` | 仅有本工作包自身产物：` M scripts/s6_gate0_fingerprint.py`、`?? reports/s6-followup/` |
| `git branch --show-current` | `release/s6-canary-hardening-v3` |
| `git rev-parse HEAD` | `5db5246010c884a8a980e8279e4b49cb164a3603` |
| `git log -12 --oneline --decorate` | 见 §3 |
| `git remote -v` | `origin`（SSH，发布远程）+ `zhaohuibin7`（HTTPS 镜像） |
| `git fetch --all --prune` | exit 1：origin OK；`zhaohuibin7` 连接被重置（不影响发布远程） |
| `git status -sb` | `## release/s6-canary-hardening-v3`（新分支无 upstream） |
| `git diff --check` | exit 0（仅 1 条 LF→CRLF 换行提示，非空白错误） |

## 2. 与上轮报告的差异

HEAD 与上轮报告基线 `5db5246` **完全一致**，无需解释差异。上轮新增的
`bdd80d6 / 863149f / b88d34a / ef318cd / 5db5246` 五条提交均在当前 HEAD 历史内，
`release/s6-canary-hardening-v2` 与 `release/s6-canary-hardening` 均保留。

唯一差异是本轮开始时存在两个**本工作包自身**的工作区变更：

1. `scripts/s6_gate0_fingerprint.py`：修复 `--out-dir` 相对路径无法
   `relative_to(ROOT)` 的问题（改为 `.resolve()`），使脚本可把证据写到
   `reports/s6-followup/gate0/`。属工具修复，不改保护资产。
2. `reports/s6-followup/`：本轮新增证据目录。

未清理任何未知文件，未执行 reset/rebase/force-push。

## 3. 提交历史

```text
5db5246 (HEAD -> release/s6-canary-hardening-v3, origin/release/s6-canary-hardening-v2, release/s6-canary-hardening-v2) docs(s6.5): record full regression, browser, performance and final gate matrix
ef318cd test(s6.3): record security regression, dependency scan and short soak
b88d34a fix(ui): end loading state when advance-decline is an empty 404
863149f feat(s6.2): add research/user db split foundation and idempotent migration
bdd80d6 feat(s6.1): harden isolated egress scripts and record gate0 isolation evidence
c6db475 (origin/release/s6-canary-hardening, release/s6-canary-hardening) docs(s6): record acceptance report with blocking gates
0f4a050 docs(release): add canary approval package with blocking gates
b044851 test(staging): deploy isolated staging, restart and rollback drill
f7a2151 test(perf): profile sql waterfall and re-run 60/60 concurrency regression
e7e710a fix(quality): return empty timeline when trade calendar is empty
b8f9905 feat(runtime): add fail-closed readiness and verify sqlite read-only semantics
6fc4611 feat(security): enforce default-deny egress allowlist with DNS and proxy guards
```

## 4. 保护资产

见 [protected-assets-before.md](./protected-assets-before.md)。本轮 `before`
指纹与上轮 `gate0` 指纹逐项对比 **差异 0 项**（C0 五脚本 git blob 与工作区
SHA256、两份冻结信号 SHA256、四个数据库 size/mtime/SHA256/integrity_check
全部一致）。

## 5. 分支策略

- 不 reset / 不 rebase / 不 force-push 已推送历史；
- 从 clean HEAD 新建 `release/s6-canary-hardening-v3`；
- `release/s6-canary-hardening-v2` 与 `release/s6-canary-hardening` 保留。

## 6. 环境限制（沿用上一轮口径）

- 开发机为 Windows 11；WSL2 存在但**不得**作为 OS 防火墙验收环境；
- 生产库/生产环境不可访问，不得写入或迁移；
- 24h soak 需要持续环境，若无法保持必须标 `NOT_RUN`。
