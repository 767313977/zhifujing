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
