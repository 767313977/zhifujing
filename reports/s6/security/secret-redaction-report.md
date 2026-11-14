# S6-A3 凭据脱敏验证

## 1. 结论

| 检查项 | 结果 | 证据 |
| --- | --- | --- |
| URL 中的 `user:password@host` 不进异常文本 | PASS | `test_url_credentials_are_redacted` |
| query 中的 `token` / `api_key` 不进异常文本 | PASS | 同上 |
| 拒绝日志只含 method + hostname + port | PASS | `_block_policy` / `_guarded_request` |
| 策略文件不含凭据 | PASS | `test_policy_inventory_has_no_credentials` |
| `stats()` / `metrics()` 不含域名或路径 | PASS | 仅计数与布尔；`authorized_ips` 只返回数量 |
| 启动日志不打 token | PASS | `main._log_quota_config` 只打「是否已配置」 |

## 2. 脱敏实现

- `external_guard._safe_host(value)`：从 URL / `host:port` 中只取 hostname；
  解析失败返回 `unknown`，**绝不回显原串**。
- `external_guard._safe_context(context)`：业务入口标签先剔除 URL / path / query /
  userinfo，再截断到 48 字符。
- `ExternalFetchBlocked` 只携带稳定 `reason_code` 与安全来源标签。
- `egress_policy` 的拒绝日志只输出 `hostname:port`，不输出完整 URL。

## 3. 测试样例

输入：

```
https://user:supersecret@example.com/path?token=abc123&api_key=xyz
```

实际输出（异常文本与日志）：

```
离线验收模式：外部数据现取被拒绝（EGRESS_DESTINATION_DENIED）；来源 example.com
出站策略拒绝 requests → example.com:443（UNKNOWN_DESTINATION）
```

`supersecret` / `abc123` / `xyz` 均未出现。

## 4. 未覆盖

- 上游库（requests/urllib3）自身在 debug 日志级别可能打印完整 URL；
  本轮未开启 debug，也未修改第三方库行为。
- 环境变量与 `.env` 中的真实 token 不属于仓库内容，未做读取或输出。
