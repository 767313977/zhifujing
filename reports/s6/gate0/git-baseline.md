# S6 Gate 0：Git 基线

- 采集时间：2026-11-15T01:11:10+08:00
- 采集主机：`HAN-LAPTOP`（Windows 11）

| 项 | 值 |
| --- | --- |
| 仓库根 | `C:/Users/76731/Documents/trae_projects/fupan` |
| 起始分支 | `release/s6-canary-hardening` |
| 起始 HEAD | `c6db475b9848fc37e703be0bd3f186c9d20d053e` |
| 新分支 | `release/s6-canary-hardening-v2`（自上述 HEAD 创建，不重写历史） |
| 远端 | `origin` = `git@github.com:767313977/zhifujing.git`；另有 `zhaohuibin7`（HTTPS） |
| 上游 | 新分支创建时无上游（`upstream=null`） |

## 起始工作区状态

`git status --porcelain` 在生成证据前的唯一改动是本次 Gate 0 工具与证据文件：

```
 M scripts/s6_gate0_fingerprint.py
?? reports/s6/gate0/...
```

没有用户未提交的代码改动，没有对既有审查分支的 reset / rebase / force-push。

## 最近提交

```
c6db475 docs(s6): record acceptance report with blocking gates
0f4a050 docs(release): add canary approval package with blocking gates
b044851 test(staging): deploy isolated staging, restart and rollback drill
f7a2151 test(perf): profile sql waterfall and re-run 60/60 concurrency regression
e7e710a fix(quality): return empty timeline when trade calendar is empty
b8f9905 feat(runtime): add fail-closed readiness and verify sqlite read-only semantics
6fc4611 feat(security): enforce default-deny egress allowlist with DNS and proxy guards
c9000da chore(s6): record gate0 baseline and protected asset fingerprints
```

机器可读版本：`reports/s6/gate0/git-baseline.json`。
