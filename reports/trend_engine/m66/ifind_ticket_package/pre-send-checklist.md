# iFinD 工单发送前检查表

由**用户**执行；TRAE 不发送工单，也不得声称已发送。

- [ ] 已通读 `ticket-body.md`，确认措辞与问题范围可接受
- [ ] 确认正文不含 Token、账号、Cookie、内网地址、本地绝对路径
- [ ] 确认只提到 3 只样本证券与「除权日前后 30 个交易日」范围
- [ ] 确认是否需要附 `field_dictionary_template.csv`（空模板）
- [ ] 确认是否附 `ifind_28_questions.csv`（可选，正文已含 28 问）
- [ ] 选择发送渠道（邮件 / 供应商门户 / 客户经理微信）
- [ ] 发送后在 `provider_reply_register.csv` 补记 `ticket_id`、发送时间与渠道
- [ ] 收到答复后，按 `sample-response-redaction-template.md` 脱敏并登记

## 发送后状态更新

| 项 | 值 |
| --- | --- |
| 发送状态 | `DRAFT_READY_NOT_SENT` → 用户发送后改为 `SENT_BY_USER` |
| 发送时间 | 由用户填写 |
| 工单号 | 由用户填写，未知保持空 |
| 答复状态 | 收到后按题更新 |
