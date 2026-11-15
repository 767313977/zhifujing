# 供应商答复脱敏与登记模板

## 1. 收到答复后的处理顺序

1. **原样保存**原始文件（邮件正文/附件/截图导出），不改写、不裁剪；
2. 计算原始文件 SHA256；
3. 复制一份并脱敏（删除 Token、账号、联系人、内网地址、价格中的个人信息）；
4. 脱敏版才允许写进报告或工单归档；
5. 在 `provider_reply_register.csv` 登记元数据；
6. 逐题更新 `question_reply_status.csv`。

## 2. 必须删除的内容清单

- [ ] Token、API Key、Authorization Header、Cookie
- [ ] 账号 ID、注册手机号、邮箱、联系人姓名
- [ ] 内网 IP、本地绝对路径（如 `C:\Users\...`）
- [ ] 供应商侧工单系统内部链接中的会话 ID
- [ ] 任何完整原始行情数据（只保留字段名与结构示意）

## 3. 登记字段（与 `provider_reply_register.csv` 一致）

| 字段 | 说明 |
| --- | --- |
| reply_id | 本系统生成，`sha256[:16]` |
| supplier | 供应商名称 |
| ticket_id | 供应商工单号；未知则空（NULL），不得编造 |
| received_at | **我方收到时间**，不得当作供应商 `available_at` |
| source_channel | email / portal / wechat / document |
| original_filename | 原始文件名 |
| raw_sha256 | 原始文件 SHA256 |
| redacted_sha256 | 脱敏版 SHA256 |
| redaction_status | PENDING / DONE |
| reviewed_by | 审阅人 |
| review_status | OPEN / REVIEWED / ACCEPTED / REJECTED |

## 4. 逐题状态取值

`OPEN` / `ANSWERED` / `PARTIAL` / `NOT_SUPPORTED` / `NEEDS_FOLLOWUP` / `VERBAL_UNCONFIRMED`

判定规则：

- 只有口头/会议答复 → `VERBAL_UNCONFIRMED`；
- 只给接口名、没有字段字典和示例 → `PARTIAL`；
- 明确回复不支持 → `NOT_SUPPORTED`；
- **未答复不等于不支持**，必须保持 `OPEN`。

## 5. 禁止

- 不得把 `received_at` 写进 `available_at`；
- 不得把供应商「销售口头报价」当授权凭据；
- 不得因答复中出现某字段名就推断其单位或方向。
