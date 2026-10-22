"""统一的「北京日期」口径（2026-10-22 体检）。

为什么不用 `date.today()`：它取的是**进程本地时区**的日期。

- 云端是腾讯云 Ubuntu，`install.sh` 里虽然 `timedatectl set-timezone Asia/Shanghai`
  且 systemd 也钉了 `TZ`，但只要哪台机器漏了其中一处（换机、容器、手工跑脚本），
  16:00 之后（UTC 08:00）就会把「昨天」当成「今天」—— 于是定点采集判成「还没到
  时刻」而跳过、启动补采补错日期，表现是「今天的数据一直不出现」，很难往时区上想。
- 中国市场没有夏令时，固定 +8 偏移就是唯一正确的北京日期，不需要 zoneinfo 数据
  （某些精简镜像缺 tzdata，`ZoneInfo("Asia/Shanghai")` 会直接抛异常）。

所以判「今天」的地方一律走 `today_cn()`，不依赖运行环境怎么配。
"""

from datetime import date, datetime, timedelta, timezone

CST = timezone(timedelta(hours=8), "Asia/Shanghai")


def now_cn() -> datetime:
    """当前北京时间（带 +08:00 时区）。"""
    return datetime.now(CST)


def today_cn() -> date:
    """当前北京日期 —— 替代 `date.today()`。"""
    return now_cn().date()
