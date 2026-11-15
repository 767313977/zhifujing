# M6.7 最终验收

- 日期：2026-11-16
- 分支：`feat/trend-engine-m67-data-gap-closure`（自 `b310d63` 创建）
- 真实供应商请求：**0**
- 费用：**0 元**
- 原始库与 M6.4 staging：未变
- **最终状态：`M67_LOCAL_WORK_COMPLETE_PENDING_DATA_AUTHORIZATION`**

---

## 1. 一句话结论

**本阶段把「缺什么、谁能给、什么许可、怎么验证」写清楚了，但没有新增任何真实数据 —— G9 与 G3 仍为 FAIL。** 契约、PIT、血缘、授权守卫、fake provider、测试均已就绪，下一步只差用户批准具体数据源。

## 2. 完成矩阵

| 批次 | 内容 | 状态 |
| --- | --- | --- |
| N0 / B0 | 基线、并发、受保护哈希、范围 | 完成 |
| N1 / B0 | M6.6 证据对账（14 事实 / 10 未决） | 完成 |
| N2 | 全仓数据调用链审计（12 数据流 / 11 风险） | 完成 |
| N3 / B2 | G9 价格域与公司行为契约 + 18 项测试 | 完成 |
| N4 / B3 | G3 成分契约 + 映射审计 + 10 项测试 | 完成 |
| N5 / B4 | PIT 与血缘契约 + 泄漏测试规范 | 完成 |
| N6 / B1 | 供应商矩阵 + 28 问工单 v2（未发送） | 完成 |
| N7 / B5 | 授权守卫 + Fake Provider + 15 项测试 | 完成 |
| N8 | 全量测试、Ruff、安全/网络/Git 审计 | 完成 |
| N9 | 本地提交 | 完成（不 push） |

## 3. 新发现（相对 M6.6）

1. **生产“前复权”由 `pct_chg` 合成**（`patterns.build_bars`），无因子血缘；8 处调用（R-01）。
2. **`pricing.py` 的双价格域契约未接入生产**，只被 `trade_sim` 引用了一个异常类型（R-02）。
3. **ST 只能按当前名称识别**，历史 ST 不可还原（R-04）。
4. b1 板块口径 374 vs 383 的差异已定位（`sector_basic` vs `sector_daily`）。
5. M6.6 两次失败请求的 `error_code=IfindAuthError` 需要与「我方路由错误」并列说明（D-01）。

## 4. Gate 状态

见 `07-m67-final-gate-matrix.csv`：G1 staging PASS、G2 PARTIAL、G3 FAIL、G9 各子项 PARTIAL/FAIL、M7 未申请。

## 5. 测试与检查

| 项 | 结果 |
| --- | --- |
| M6.7 新增测试 | 45 项（g9 18 / g3 10 / authz 15 + 参数化用例） |
| 后端全量 | **2018 passed, 4 skipped**（391.04 秒） |
| Ruff | All checks passed |
| mypy | `NOT_RUN`（环境未安装） |
| 网络请求 | 0 |
| 四个库 SHA | 一致 |
| M6.4 staging | 16/16 |
| 敏感信息 | 未发现 |

## 6. 未解决（需授权）

| issue | 严重度 | 需要谁 |
| --- | --- | --- |
| 分红/送转值为空 | P0 | iFinD 书面答复 / Tushare / 理杏仁 |
| 无 adjust_type 与因子定义 | P0 | 供应商字段字典 |
| 300750 事件未决 | P0 | 第二家供应商 |
| 板块无历史截面 | P0 | Tushare 6000 积分 / Choice |
| 网站商业许可 | P0 | 书面许可 |
| 额度余额无 API | P1 | 供应商用量接口 |

## 7. 用户需批准的最小事项

1. **发送 iFinD 工单 v2**（`06-ifind-28-question-ticket-v2.md`）；
2. **Tushare 2000 积分试用**（约 200 元/年）→ 补分红/送转与因子交叉验证；
3. **Tushare 6000 积分或 Choice 试用** → G3 历史成分；
4. **理杏仁 API 试用** → 配股与分红明细；
5. 是否允许把 M6.6 推送报告与 M6.7 提交 **push 到远端**（当前仅本地）。

## 8. 强制声明

- Model A / B / A+B **仍冻结**；未训练、未调参、未回测优化。
- **不申请 M7**。
- G1 staging PASS ≠ 研究库解锁；本阶段未合并任何 staging。
- M6.7 结束**不自动授权**数据合并、网站展示或再分发。
- `WEBSITE_COMMERCIAL_LICENSE = NOT_ESTABLISHED`。
- 本阶段文档与 fake provider 的完成，**不等于**真实数据 Gate 通过。
