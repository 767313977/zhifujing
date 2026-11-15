# S6.8/S6.9 Git 推送状态

- 基线：`fix/s6-7-login-spa-transition` / `a6e9dbda939051b38b822571db0ba003addf40d0`
- 工作分支：`work/s6-8-s6-9-gate-closure`
- 提交链：
  - `6d556e4` fix(ui): bound chunk reload and add dev-only error fixture
  - `c22378b` test(s6.8): add cdp browser, versioned staging and sql profiling tools
  - `e8fbecf` test(s6.8): record browser, versioned rollback, egress and soak evidence
  - `eb6024a` test(s6.8): include raw regression and staging logs
  - `36c698e` chore(s6.8): refresh gate0 final after evidence commits
- 首次推送：`git push -u origin work/s6-8-s6-9-gate-closure` → 成功（新建远端分支）
- 远端校验：`origin/work/s6-8-s6-9-gate-closure` = `36c698e614ab010e9758ccc9899e8fd0463c3475`
  （与本文件生成时本地 HEAD 一致）
- 本文件单独提交后再推送；最终 HEAD 以 `git rev-parse HEAD` 与远端同名为准
- PR 入口：`https://github.com/767313977/zhifujing/pull/new/work/s6-8-s6-9-gate-closure`
- 未 amend、未 force push、未合并 master、未部署生产

## 2026-11-15 追加：Hyper-V 主机预检与 VM 工具

- `a24a3a6` feat(s6.8): add Hyper-V host preflight and VM isolation tooling
- `bf430e0` test(s6.8): record Hyper-V host preflight and script static checks
- 推送：`git push` 成功，`origin/work/s6-8-s6-9-gate-closure` = `bf430e023b6d014e6f9b9a47ba6d259fefb177b7`
- 状态：Hyper-V 管理面 PASS；VM 创建/N01–N24 仍 `NOT_RUN`（缺 Ubuntu ISO）
- 本轮未创建 VM、未创建 vSwitch/NAT、未修改宿主防火墙

## 2026-11-15 追加：VM 创建（s6-egress-vm）

- `95fb6a8` fix(s6.8): bound VM heartbeat wait for installer phase
- `5bf2ee2` test(s6.8): create isolated Hyper-V VM and record isolation evidence
- 推送：`git push` 成功，`origin/work/s6-8-s6-9-gate-closure` = `5bf2ee2212a57f0ae2a35d810c028c6d0c53fecc`
- ISO SHA256 本地重算一致；VM Gen2 / 4 vCPU / 6 GB / 64 GB Dynamic / Internal `s6-egress` / NAT 172.31.240.0/24
- 宿主防火墙规则 769→769、无 `s6*` 规则；WLAN IP 未变
- 当前暂停，等待 vmconnect 手动安装 Guest；未执行 bootstrap/N01–N24

## 2026-11-15 追加：平衡方案（C）收尾

- `a3ff686` fix(security): block private and metadata addresses in egress guard
- `8a0fb81` test(s6.9): add backup restore drill, resource monitor and soak timeline
- `6a45ce7` test(s6.8/s6.9): record balanced gates, short smoke and backup restore
- 推送：`git push` 成功，`origin/work/s6-8-s6-9-gate-closure` = `6a45ce79e96adac6ff294aa5b5e89b9892bf3d2b`
- 状态：S6.8 PARTIAL / S6.9 PARTIAL（短 Smoke PASS；24h NOT_RUN）；OS Egress DEFERRED
- VM 仍 Running、Guest 未安装；未删除/未强停任何 Hyper-V 资源
