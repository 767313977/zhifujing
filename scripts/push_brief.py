"""把当日复盘简报推到飞书的命令行入口。

发送通道由 `.env` 决定，优先群机器人 webhook（长期有效），
没配时才退回 `lark-cli`（靠 Trae 注入的 2 小时 Token，会随后端运行时长失效）。

这个脚本是**手工补发**与 Trae 定时任务的入口：它每次都是新进程，
即便主通道是 lark-cli 也拿得到新 Token。

两条通道共用 `collect_log` 里的 `push_brief` 记录去重，所以谁先成功谁发，
不会收到两份。要强制重发用 `--force`。

用法::

    python scripts/push_brief.py                  # 今天没推过才推
    python scripts/push_brief.py --print          # 只把简报打出来，不发送
    python scripts/push_brief.py --force          # 无条件重发（手工补发用）
    python scripts/push_brief.py --date 2026-09-17
"""

import argparse
import logging
import sys
from datetime import date
from functools import partial
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import get_settings  # noqa: E402 - 依赖上面改 sys.path
from app.jobs.push_brief import (  # noqa: E402
    CALIBRATION_TASK,
    PUSH_PATTERNS,
    PUSH_TASK,
    already_pushed,
    build_brief,
    build_calibration_reminder,
    build_pattern_brief,
    latest_trade_date,
    push,
    push_calibration_reminder,
    push_pattern_brief,
)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期要写 YYYY-MM-DD，收到 {value}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="推送当日复盘简报到飞书")
    parser.add_argument("--date", type=_parse_date, help="交易日，缺省取库中最新一天")
    parser.add_argument("--print", dest="print_only", action="store_true", help="只打印，不发送")
    parser.add_argument("--force", action="store_true", help="当天推过也再推一次")
    parser.add_argument(
        "--gongda",
        action="store_true",
        help="只处理「共达模式选股」那条独立消息（当日无命中则不发送）",
    )
    parser.add_argument(
        "--oneil",
        action="store_true",
        help="只处理「欧奈尔突破」那条独立消息（当日无命中则不发送）",
    )
    parser.add_argument(
        "--calibration",
        action="store_true",
        help="只处理「配额对账提醒」",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    target_date = args.date or latest_trade_date()
    if target_date is None:
        print("库里还没有任何交易日数据，先跑一次采集")
        return 1

    # 每条消息各有各的内容、发送函数与去重任务名
    if args.calibration:
        task, label, sender = CALIBRATION_TASK, "配额对账提醒", push_calibration_reminder
        markdown = build_calibration_reminder(target_date)
    elif args.gongda or args.oneil:
        # 形态清单在 backend 的 PUSH_PATTERNS 里；partial 把形态 key 绑进发送函数
        pattern = "limit_surge_flat" if args.gongda else "oneil_breakout"
        label = PUSH_PATTERNS[pattern][0]
        task = PUSH_PATTERNS[pattern][1]
        markdown = build_pattern_brief(target_date, pattern)
        sender = partial(push_pattern_brief, pattern)
    else:
        task, label, sender = PUSH_TASK, "复盘简报", push
        markdown = build_brief(target_date)

    if args.print_only:
        print(markdown or f"（{target_date} 的{label}没有内容，不会发送）")
        return 0

    if not markdown:
        print(f"{target_date} 的{label}没有内容，不发送")
        return 0

    if not args.force and already_pushed(target_date, task):
        # 另一条通道已经发过了。返回 0 而不是报错：这是正常情况，不是失败。
        print(f"{target_date} 的{label}已推送过，跳过（要重发加 --force）")
        return 0

    settings = get_settings()
    if not settings.feishu_webhook_url.strip() and not settings.feishu_target.strip():
        print("未配置 FEISHU_WEBHOOK_URL（群机器人）或 FEISHU_TARGET（收件人），先写进 .env")
        return 1

    result = sender(target_date, settings)
    if result["status"] == "ok":
        channel = (
            "群机器人 webhook"
            if settings.feishu_webhook_url.strip()
            else settings.feishu_target
        )
        print(f"{label}已推送到 {channel}，{result['chars']} 字")
        return 0
    print(f"推送失败：{result.get('reason') or result.get('error')}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
