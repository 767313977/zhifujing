# S5 浏览器验收证据

- 环境：临时 SQLite `%TEMP%\s5_e2e\s5.db`，临时账号 `s5a` / `s5b`；
- 后端：`127.0.0.1:8014`，`SCHEDULER_ENABLED=false`、`EXTERNAL_DATA_FETCH_ENABLED=false`、`WARMUP_PATTERN_CACHE=false`、`FUPAN_SCHEMA_MODE=verify`；
- 浏览器：Chrome headless（CDP），视口 390x844 / 768x1024 / 1280x800 / 1920x1080 / 3840x2160；
- 总计：45 项，通过 45 项。

## 用例结果

| 结果 | 用例 | 说明 |
| --- | --- | --- |
| PASS | theme_panel_ok | 题材交叉仍可用 |
| PASS | resonance_buckets | buckets=True up=True down=True again50=True |
| PASS | resonance_and_or_dedupe | and2=True or5=True |
| PASS | resonance_and_unverifiable | A01+A02+A04 -> UNVERIFIABLE |
| PASS | resonance_zero_sample | 09-21 无成员快照 -> 未采集，不伪造 0 只 |
| PASS | resonance_viewports | 1920/3840/390 screenshots captured |
| PASS | quality_overview | table=True reason=True |
| PASS | quality_detail_expand | 展开行显示原因码/最近尝试 |
| PASS | quality_filter | 筛选后隐藏可信来源 |
| PASS | quality_uncollected_day | 09-22 显示未采集 |
| PASS | quality_viewports | 1920/3840/390 screenshots captured |
| PASS | research_three_buckets | 三区标题存在 |
| PASS | research_later_not_in_known | known_has_theme=False unknown_has_theme=True later_label=False unknown_label=True |
| PASS | research_forward_outcome | D+1 -5.00% 不在已知区 |
| PASS | research_end_of_day_fact | 日终事实与 15:00 截止时刻可见 |
| PASS | drawer_escape | Escape closes |
| PASS | drawer_refresh_asof | 刷新保留 stock/as_of |
| PASS | drawer_history | back_closed=True forward_open=True |
| PASS | limit_page | /limit-up 仍有个股链接 |
| PASS | auction_page | /auction |
| PASS | sentiment_page | /sentiment |
| PASS | workbench_page | 工作台页面加载 |
| PASS | workbench_sections | 情绪/漏斗/共振/次日/质量五区可见 |
| PASS | workbench_and_total | AND A01+A02 = 2 只 |
| PASS | workbench_boundary | 已观测边界与只读声明可见 |
| PASS | workbench_url_sync | 输入回填=A01,A02 |
| PASS | workbench_or_dedupe | OR A01+A02 = 5 只（去重） |
| PASS | workbench_and_unverifiable | A04 无快照 -> UNVERIFIABLE |
| PASS | workbench_d1_pending | 无下一交易日 -> 待观察，不写 0% |
| PASS | workbench_quality_view | 质量视图隐藏梯队/次日面板 |
| PASS | workbench_empty_sectors | 未选板块仍显示情绪/漏斗/质量 |
| PASS | workbench_stock_link | 逐券行保留个股链接 |
| PASS | workbench_quick_research_evidence | 「据」直达 evidence Tab |
| PASS | workbench_viewports | 390/768/1920/3840 screenshots captured |
| PASS | account_switch_clears_drawer | user_b=True no_drawer=True |
| PASS | offline_gate_no_network | before_sent=0 after_sent=0 |
| PASS | offline_probe_note | status=200 note_offline=True |
| PASS | offline_sync_blocked_before_network | status=503 blocked=1 sent=0 |
| PASS | research_pool_pit_no_leak | 盘后池成员只进未知区，不进已知区 |
| PASS | research_time_policy_visible | naive 时间策略在页面显式可见 |
| PASS | research_pool_unconfirmed_membership | 未知区只展示未证明成员；未证明成员与已证明成员分开 |
| PASS | workbench_quality_two_layers | source / combined 两层状态 + data_as_of 语义 |
| PASS | egress_tls_sendfile_guards | guards={'requests': True, 'socket_connect': True, 'socket_stream': True, 'socket_sendmsg': False, 'socket_sendfile': True, 'ssl_stream': True, 'ssl_sendfile': True} stream=1297 sendfile=0 |
| PASS | egress_effective_state | enabled=False locked=True dns=NOT_INTERCEPTED udp_blocked=0 |
| PASS | console_errors | 0 errors |

## 截图 SHA256

| 文件 | SHA256 | 字节 |
| --- | --- | --- |
| resonance_1280x800.png | `6a214dfd57eb823db2276fd9c2988f406fccf90b822f10c3b0247752e18b26ee` | 146916 |
| resonance_unverifiable_1280x800.png | `0ef6541c9f9849dfb78db40403a141fd6de9e893d1a9d85c0bd8bd4253db9f8d` | 151857 |
| resonance_1920x1080.png | `a101f760a1c5ba5a84e3e3dda490dc69c54f3b78e5703672d7c61d4a1ba1f0f4` | 167638 |
| resonance_3840x2160.png | `ee77db35d548fa046af04dd8965ece7eabc934c43ba0661786a460c3eb0fa5e4` | 339429 |
| resonance_390x844.png | `3caf90eab96031ffe48b330458bcd55799e27fdc21d25bd5e06d6468d44f8393` | 59259 |
| quality_1280x800.png | `b058d59bf8e5a283e74d712c8c74f5d3354a076f866630ceaddf60afae4254c0` | 102234 |
| quality_1920x1080.png | `4beb0271be0dd156917862c97dc2108e911955e7f3148ab75a58dfe3e8e2ec6d` | 102933 |
| quality_3840x2160.png | `5a5508ba5d7071ad50f5e93e0994d4ea7b17ec57469093c49108887d88aa9dca` | 127121 |
| quality_390x844.png | `46f297dff192c8ba89c81a9dc935bf4155e87452650cd41a4edeac1755c8e3e1` | 57734 |
| research_three_buckets_1280x800.png | `96aebf7d31172cab9abf5b88fc446c0f4d3dca38f828e2aad57de183356b422d` | 164369 |
| workbench_1280x800.png | `439fca812ee34e53acfc79d329e7a003cf1ba1a06ed3bc1887d01c005e0685db` | 114694 |
| workbench_pending_1280x800.png | `1ac16f4e6b11da01dee6c43fc4744be02abc9f8c49d78ec2b876fdcbbc561552` | 108367 |
| workbench_390x844.png | `bd27f59c7013e3f9c95d84d1ac967271fff0511f631f082584c41854d5b7a5d3` | 58385 |
| workbench_768x1024.png | `7841f82592f06c70d49b099d99d5350ae588723e9c21ec5fb890a9efbfb3bd05` | 95750 |
| workbench_1920x1080.png | `307bd6e142939689c95e46bf6d99b638187170b77897750c2eb514fbac653765` | 148494 |
| workbench_3840x2160.png | `00aef8800e91e7c99dfb8d8b72c67a45079e8821807ddef55d26b3b80626c010` | 268972 |

## 复现命令

```powershell
# 1) 造临时库（临时脚本，不提交）
.venv\Scripts\python.exe %TEMP%\s5_e2e\seed.py
# 2) 起隔离后端（四个开关都在启动前设置）
$env:DB_PATH="%TEMP%\s5_e2e\s5.db"; $env:SCHEDULER_ENABLED="false"; $env:EXTERNAL_DATA_FETCH_ENABLED="false"; $env:WARMUP_PATTERN_CACHE="false"; $env:FUPAN_SCHEMA_MODE="verify"
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8014
# 3) 跑 Chrome headless
.venv\Scripts\python.exe %TEMP%\s5_e2e\drive.py
```

> 临时 harness（seed/drive）只用于本地验收，不提交；提交的是结果、截图哈希与 egress 计数。