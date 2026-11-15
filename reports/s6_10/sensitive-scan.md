# S6.10 敏感信息扫描

- 时间：2026-11-15
- 范围：`reports/s6_10/` 全部证据 + 本轮代码 diff（`external_guard`、`config`、
  `scheduler`、`stock_phase`、`install.sh`、`egress-policy.json`）

## 扫描模式

```text
IFIND_AUTH_TOKEN=<值>
FEISHU_WEBHOOK_URL=<值>
OPENAI_API_KEY=<值>
BEGIN RSA/EC PRIVATE KEY
Authorization: <值>
Bearer <token>
Set-Cookie: <值>
password=<值> / 明文密码
```

## 结果

| 范围 | 命中 |
| --- | --- |
| `reports/s6_10/**` | 0 |
| 代码 diff（6 个文件） | 0 |
| 新增脚本 | 0（仅测试口令常量 `staging-password-123`，为既有隔离测试账号，不含生产凭据） |

## 说明

- 本报告与全部证据**不含**生产 `.env` 内容、Token、Webhook、私钥、Cookie 原文；
- 浏览器证据只保留 DOM 文本、路径、尺寸与截图，不含 Cookie/Token；
- `reports/s6_10/production-config-audit.md` 只写“是否设置”，不写具体值。
