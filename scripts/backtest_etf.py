"""ETF 动量轮动回测（2026-10-31 加，ETF-4）。

**这是研究脚本，不属于站点本身。** 选股与 `api/etf/track` 完全同源
（`etf_track.rotation` → `etf_screen.select`），区别只是「每天重选」换成
「每隔 `step` 个交易日换一次仓」，并扣掉交易成本。

口径：

- 调仓日 `t` 收盘出信号 → `t+1` 收盘换仓 → 持有到 `t+1+step` 收盘；两段首尾相接；
- 成本**单边万 0.5**（`--cost 0.00005`），换手率 = 买入权重 + 卖出权重（全换 = 2），
  ETF 无印花税；
- 基准两个：510300（沪深300ETF）与「去重后全体等权」，同一建仓 / 卖出日；
- 只用当时的数据（去重代表按当时的 20 日均额选），没有未来函数。

    python scripts/backtest_etf.py                       # 动量：周/双周 × top 3/5/10
    python scripts/backtest_etf.py --presets momentum,breakout,defensive,t0   # 四个预设一起比
    python scripts/backtest_etf.py --steps 5 --tops 5 --days 120

⚠️ 样本只有 `etf_bar` 里当前这一段（2025-10 起，约 250 个交易日），而且**同指数去重
后只剩约 900 只代表**；结果只能用来横向比较「周期 × 集中度」，不能当长期能力。
`inflow` 预设依赖净申赎（份额 2026-09-21 才有），目前跑不出完整样本。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.db import session_scope  # noqa: E402
from app.services import etf_screen, etf_track  # noqa: E402


def _fmt(value: float | None, suffix: str = "") -> str:
    return "—" if value is None else f"{value:+.2f}{suffix}"


def main() -> int:
    parser = argparse.ArgumentParser(description="ETF 轮动回测（调生产筛选口径）")
    parser.add_argument(
        "--preset",
        default="",
        help="单个预设（兼容旧写法；等价于 --presets 只传一个）",
    )
    parser.add_argument(
        "--presets",
        default="momentum",
        help="预设，逗号分隔：momentum / breakout / inflow / defensive / t0",
    )
    parser.add_argument("--steps", default="5,10", help="调仓周期（交易日），逗号分隔")
    parser.add_argument("--tops", default="3,5,10", help="持仓只数，逗号分隔")
    parser.add_argument(
        "--days", type=int, default=etf_track.MAX_DAYS, help="回看多少个交易日（默认全部）"
    )
    parser.add_argument(
        "--cost", type=float, default=etf_track.DEFAULT_COST, help="单边成本（默认 0.00005 = 万 0.5）"
    )
    args = parser.parse_args()

    presets = [value.strip() for value in (args.preset or args.presets).split(",") if value.strip()]
    unknown = [value for value in presets if value not in etf_screen.PRESETS]
    if unknown:
        raise SystemExit(f"未知预设：{unknown}；可选：{', '.join(etf_screen.PRESETS)}")
    steps = [int(value) for value in args.steps.split(",") if value.strip()]
    tops = [int(value) for value in args.tops.split(",") if value.strip()]

    with session_scope() as session:
        for preset in presets:
            print("=" * 108)
            print(
                f"ETF 轮动回测：{etf_screen.PRESETS[preset]}（{preset}） · "
                f"回看 {args.days} 个交易日 · 单边成本 {args.cost * 10000:.2f}‱ · 无印花税"
            )
            print(
                f"{'调仓':<8}{'持仓':>4}{'期数':>6}{'均值':>9}{'中位':>9}{'胜率':>8}"
                f"{'等权基准':>10}{'超额':>9}{'最好':>9}{'最差':>9}{'累计':>10}{'510300累计':>12}"
            )
            print("-" * 108)
            for step in steps:
                for top in tops:
                    result = etf_track.rotation(
                        session,
                        preset=preset,
                        top=top,
                        step=step,
                        days=args.days,
                        cost=args.cost,
                    )
                    stats = result.stats
                    if not stats["samples"]:
                        print(
                            f"{step:<8}{top:>4}{0:>6}{'—':>9}{'—':>9}{'—':>8}{'—':>10}"
                            f"{'—':>9}{'—':>9}{'—':>9}{'—':>10}{'—':>12}"
                        )
                        continue
                    print(
                        f"{step:<8}{top:>4}{stats['samples']:>6}"
                        f"{_fmt(stats['mean']):>9}{_fmt(stats['median']):>9}"
                        f"{stats['win_rate']:>7.1f}%"
                        f"{_fmt(result.bench['universe']['mean']):>10}"
                        f"{_fmt(result.bench['excess']['mean']):>9}"
                        f"{_fmt(stats['best']):>9}{_fmt(stats['worst']):>9}"
                        f"{_fmt(stats['total']):>10}"
                        f"{_fmt(result.bench['hs300']['total']):>12}"
                    )
    print("=" * 108)
    print("（收益单位 %，未年化；超额 = 策略期均值 − 同期去重后全体等权均值；累计按逐期复利）")
    print("（⚠️ 样本只有一段行情（约 250 个交易日），只能横向比较预设 / 周期 / 集中度）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
