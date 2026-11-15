# M6.7 N1：M6.6 证据全量对账

- 日期：2026-11-16
- 范围：M6.6 的 13 份交付 + 工单包 + 台账 + 账本
- 结论：**主要结论一致，发现 1 处措辞级差异、1 处口径需并列说明；无事实冲突**
- 明细：`01-m66-fact-register.csv`（14 条事实）、`01-m66-open-issues.csv`（10 条未决）

---

## 1. 十二项专项核对

| # | 核对项 | 结果 | 说明 |
| ---: | --- | --- | --- |
| 1 | 42 次请求与账本逐条合计 | **一致** | 账本 42 行，序号连续 1~42 |
| 2 | 6 能力 / 30 G9 / 6 G3 | **一致** | 账本 purpose 计数完全吻合 |
| 3 | 两次失败请求标注 | **基本一致，需并列说明** | 账本 `provider_error_code=IfindAuthError`；根因是 `sector_data` 发到 stock server（我方路由错误）。见第 2 节 |
| 4 | 100 次守卫测试与真实计数区分 | **一致** | 守卫测试使用 tmp 目录；真实计数仅本批次账本 |
| 5 | `DRAFT_READY_NOT_SENT` 仍准确 | **一致** | `package_manifest.json`、`01-ifind-ticket-package-review.md`、`pre-send-checklist.md` 共 4 处一致 |
| 6 | 13 条第三方 `NOT_AUTHORIZED` | **一致** | 台账 18 行：13 NOT_AUTHORIZED + 4 AUTHORIZED_LIMITED(iFinD) + 1 REJECTED |
| 7 | iFinD 四条 `AUTHORIZED_LIMITED` 范围 | **一致** | 均限定在 M6.4 已执行的最小样本；本轮未扩大 |
| 8 | iFinD G3 `REJECTED` 不是许可问题 | **一致** | M6.6 文档明确「账号工具集中无该接口」 |
| 9 | `WEBSITE_COMMERCIAL_LICENSE=NOT_ESTABLISHED` 贯穿 | **一致** | M6.6 目录 6 处出现 |
| 10 | 1.8883bp 描述 | **一致** | 2 处均表述为「有限样本数值吻合」，未写成因子定义通过 |
| 11 | 300750 保持未决 | **一致** | `UNRESOLVED_EVENT_CLASSIFICATION` |
| 12 | 当前板块名单未被写成历史成分 | **一致** | 全部标 `CURRENT_SNAPSHOT_ONLY` |

## 2. 需要并列说明的差异（不修改原文）

### D-01 两次失败请求的错误码

- **账本事实**：`sequence_no 26/27`，`endpoint=stock/sector_data`，`response_status=ERROR`，`provider_error_code=IfindAuthError`。
- **报告表述**：M6.6 `03-g3-real-sample-validation.md` 称「把 `sector_data` 发到了 stock server」。
- **判定**：两者不矛盾——工具在错误 server 上返回鉴权类错误，客户端归为 `IfindAuthError`。但独立读者可能把它误读为「账号无权限」。
- **建议**：M6.7 起在报告与台账中同时记录 `root_cause=CLIENT_ROUTING_ERROR` 与 `provider_error_code`。**不改写 M6.6 原文。**

### D-02 请求数口径

- 账本 42 次包含 2 次失败与 3 次指标复算；报告在「实际 42 次」下另列「成功 40 / 失败 2」。
- 判定：一致。需注意「42」是**预占计数**，不是「42 次有用数据」；G9 有效样本请求实际少于 30。

## 3. M6.6 事实等级划分

| 等级 | 事实示例 |
| --- | --- |
| 真实响应（A） | 1.8883bp、52 行同列、分红字段为空、300176 配股数值、941/418 条当前成分 |
| 真实工具清单（A-） | stock 10 / index 3 |
| 本地台账（B） | 18 行审批台账、42 行请求账本 |
| 公开文档（C） | Tushare / 理杏仁 / Choice 的字段与门槛 |
| 未验证推断（D） | 额度余额 2,940（截图）、Tushare 因子为累计后复权（第三方解读） |

## 4. 与 M6.7 的关系

- M6.6 的结论**不需要修正**；M6.7 只增加契约、审计与验证设计。
- 上述 D-01/D-02 已写入 `01-m66-open-issues.csv`（I-08、I-06）。
