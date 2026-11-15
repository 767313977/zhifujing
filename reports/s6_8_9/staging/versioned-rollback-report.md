# S6.8 B 包：真实 OLD/NEW 版本 Staging 切换与回滚

- 时间：2026-11-15T13:23:20+08:00
- OLD：`9838a068a766f2fee4f46c6d7ebc6dfed4d80f0c`（manifest `b74306ce53c38148d5690a0adfd9e8913aef17eea2378131411e5d18e1072d6c`）
- NEW：`a6e9dbda939051b38b822571db0ba003addf40d0`（manifest `8eb65c3c9976b6b1531499b7b6f0a9d4b2b6b87b8785edcf6bceb867e8a1cb26`）
- 端口：8012（独立临时库 `C:\Users\76731\AppData\Local\Temp\s6_8_staging_6yagjtwr\staging.db`）

## 步骤

| 步骤 | 期望 | health | ready | serving entry SHA | 登录 smoke |
| --- | --- | --- | --- | --- | --- |
| 01-old | blank | 200 | 503 | `5aacf7a72e59861e48e95a6adff0be80f8441e1891d49edfe4c07df1bf339fdf` | True |
| 02-new | success | 200 | 503 | `2c72e60b87cc4e9148fdef57bc630adcf48d1f8b180db6e089f5e51cc6f1950c` | True |
| 03-rollback-old | blank | 200 | 503 | `5aacf7a72e59861e48e95a6adff0be80f8441e1891d49edfe4c07df1bf339fdf` | True |
| 04-redeploy-new | success | 200 | 503 | `2c72e60b87cc4e9148fdef57bc630adcf48d1f8b180db6e089f5e51cc6f1950c` | True |

## 结论

- OLD/NEW manifest 不同：**True**
- 实际 serving 在 OLD→NEW→OLD→NEW 间切换：**True**
- 旧版已知白屏复现（版本对照）：**True**
- 新版登录/刷新/深层链接/退出 smoke：**True**
- 总判定：**PASS**

> 单端口顺序切换，不是生产发布系统/负载均衡回滚；OS egress 未验证。
