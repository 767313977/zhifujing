# S6-E4 Staging 浏览器验收

- 目标：本地隔离 Staging `http://127.0.0.1:8010`（独立目录/端口/临时库，非生产）
- 方式：真实浏览器自动化（TRAE 浏览器代理）+ 真实运行的 `frontend/dist`
- 结果：**两轮均 PASS**
- 截图：登录页三个视口已落盘并计算 SHA256；**登录后的页面截图未能导出**
  （本机 Chrome 的 CDP 端口在该环境不可用，浏览器代理的截图未落盘）→ 见 §4 限制

## 1. 第一轮（timeline 修复前）

| 结果 | 用例 | 证据 |
| --- | --- | --- |
| PASS | 未登录访问 `/data-quality` 跳 `/login` | 地址栏为 `/login`，显示用户名/密码表单 |
| PASS | 真实登录 | `POST /api/auth/login` 200，顶栏出现 `s6staging` |
| PASS | 数据质量页渲染 | 6 个来源均显示「未采集」与原因码 |
| PASS | console error | 0 条（注入 `console.warn` 对照可被抓到，排除假阴性） |
| **FAIL（已修复）** | 来源时间线 | `GET /api/quality/timeline?source=limit_pool&days=30` 返回 **400**（`min() iterable argument is empty`），页面显示「加载失败」 |

修复：`data_quality.timeline` 在交易日历为空时返回**结构完整的空时间线**（200 + `points=[]`），
并新增 `tests/test_s6_quality_empty_timeline.py`。

## 2. 第二轮（修复后）

| 结果 | 用例 | 证据 |
| --- | --- | --- |
| PASS | 登录并跨页保持会话 | `auth/me` 200、`watchlist` 200 |
| PASS | `/data-quality` | 6 来源「未采集」；时间线 200，显示空态 `— ~ —`，**无「加载失败」** |
| PASS | `/research/workbench` | 页面骨架正常；空库下提示「交易日历为空，无法确定观察日」（见 §3） |
| PASS | `/auction` | 空态「0 只有竞价数据」 |
| PASS | `/limit-up` | 空态「0 只涨停 / 当日涨停数据未入库」 |
| PASS | `/sentiment` | 空态「0 / 0 天 / 情绪数据未入库」 |
| PASS | console error | 5 页均为 0 |

## 3. 已知非阻断项

- `/api/research/workbench` 在**空交易日历**的 staging 库上返回 400 并显示
  「交易日历为空，无法确定观察日」。这是准确的错误说明，不是把未采集伪装成 0；
  生产库有交易日历，本轮不修改该接口语义，仅在审批材料披露。

## 4. 截图 SHA256

| 文件 | 字节 | SHA256 |
| --- | --- | --- |
| login_1280x800.png | 15039 | `048C6BA14B0C858FCFC0BDED3E1B5DEBB8133766E314FB40160CAC34CFD3EF81` |
| login_1920x1080.png | 22618 | `C97611837CD999593B426677C23C7EF3AEBB2540F8C4EF835137548E205A56B6` |
| login_390x844.png | 12815 | `05D947413425DC807E5BB3905C3277921CCFCEC4B1714FB1EEF2B3354B2170C0` |

## 5. 限制（必须披露）

1. 本机 Chrome 的 `--remote-debugging-port` 在该环境不监听，CDP 脚本无法导出
   登录后页面截图；登录后页面证据来自浏览器代理的真实 DOM/Resource Timing 记录。
2. 这是 **staging smoke**，不等于 S5 的 45 项全量浏览器验收；S5 结果仍作为历史证据。
3. 空库下只能验证「状态显示正确」，不能验证业务数值正确。
4. 浏览器验收不是 OS 级网络隔离证据。
