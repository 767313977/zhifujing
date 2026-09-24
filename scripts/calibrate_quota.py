"""把本地配额记录对齐到 iFinD 后台显示的数字。

用法::

    python scripts/calibrate_quota.py 838

## 为什么需要它

iFinD 的配额是**账号级**的 —— 任何来源的调用都计入（自己在客户端手动查、手机 App、
别的程序）。本地只记得到**由这个程序**发出去的调用，所以通常比后台少。

而配额守卫唯一不能犯的错就是**低估**：低估会让它在该让路时不动，然后直接撞墙。

## 做法

把差额记成一条 `manual/calibration` 的用量记录，落在**本周期第一天**。
这样它只在本周期内被统计 —— 周期一滚（下个订阅日）自动失效，不用手动清。

重复跑是安全的：它覆盖上一次的校准值，而不是累加。
"""

import argparse
import sys
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects.sqlite import insert as sqlite_insert  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.models import IfindUsage  # noqa: E402
from app.services.usage import quota_status  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="把本地配额记录对齐到 iFinD 后台的数字")
    parser.add_argument(
        "backend_calls",
        type=int,
        help="iFinD 后台「计量区间」那一行显示的已用次数（如 838）",
    )
    parser.add_argument(
        "--date",
        help="补录落在哪一天，缺省用本周期第一天（iFinD 的订阅日）",
    )
    args = parser.parse_args()

    data = quota_status()
    local = data["cycle_calls"]
    start = date.fromisoformat(args.date) if args.date else date.fromisoformat(str(data["cycle_start"]))

    # 上次的校准值必须先扣掉：`cycle_calls` 是「本周期所有用量记录的合计」，里面**已经含**
    # 上一次写进去的那条 `manual/calibration`。直接拿 `后台 - 本地` 当差额，等于把上次的校准
    # 又当成本程序的真实调用减了一遍 —— 重复校准会**越校越少**，而少记正是配额守卫唯一
    # 危险的方向（低估 → 该让路时不动作 → 撞墙）。
    # 实测 2026-09-23：本机记录 1507（含上次校准 355）、后台 4689，按老算法写完得到 4334。
    with session_scope() as session:
        previous = (
            session.scalar(
                select(IfindUsage.calls).where(
                    IfindUsage.usage_date == start,
                    IfindUsage.server == "manual",
                    IfindUsage.tool == "calibration",
                )
            )
            or 0
        )
    real = local - previous
    diff = args.backend_calls - real
    print(
        f"本周期 {start} 起：本地记录 {local} 次（含上次校准 {previous} 次，即程序自己发了 {real} 次），"
        f"后台 {args.backend_calls} 次，差额 {diff:+d}"
    )

    if diff == 0:
        print("已经对齐，不用动")
        return 0
    if diff < 0:
        # 不自动往回扣：**多记是安全方向**（早让路而已），少记才是危险方向。
        # 真出现负数，多半是两边看的不是同一个周期，值得人工核对。
        print("本地比后台还多 —— 不自动往回扣，请先核对是不是同一个计量区间")
        return 1

    with session_scope() as session:
        session.execute(
            sqlite_insert(IfindUsage)
            .values(usage_date=start, server="manual", tool="calibration", calls=diff)
            .on_conflict_do_update(
                index_elements=[IfindUsage.usage_date, IfindUsage.server, IfindUsage.tool],
                set_={"calls": diff},
            )
        )
    print(f"已补录 {diff} 次到 {start}，现在本地 = {quota_status()['cycle_calls']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
