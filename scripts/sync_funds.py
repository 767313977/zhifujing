"""补采 / 回补资金面数据（两融、沪深股通成交额、ETF 份额、龙虎榜机构席位）。

日常由 17:30 的定时采集覆盖，这个脚本用于三种情况：
- 首次部署后补历史（EDB 支持长区间，一次调用就能取回一整段）
- 某天采集失败后手工补
- 单独重采某一天

**ETF 份额无法回补**：akshare 只提供当日快照，没有历史份额接口 ——
所以那条申赎曲线只能从第一次采集那天开始积累。这不是脚本的限制，是数据源的限制。
也正因为如此，**ETF 那一项不受 `--date` 影响**：它落在数据源自带的「数据日期」上，
盘前/凌晨/周末取到的就是上一交易日的快照。

`--days` 同时管四项：两融与北向是 iFinD EDB（每 90 天一段），机构席位是 akshare
区间（**整年一次调用**，约 22 秒），所以把 `--days` 开大几乎不加钱。

用法：
    python scripts/sync_funds.py                  # 补最近 30 天
    python scripts/sync_funds.py --days 380       # 补一年（两融/北向/机构席位都能补，ETF 补不了）
    python scripts/sync_funds.py --date 2026-09-18
"""

import argparse
import logging
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sqlalchemy import select  # noqa: E402

from app.db import init_db, session_scope  # noqa: E402
from app.jobs import collect_funds  # noqa: E402
from app.models import TradeCalendar  # noqa: E402
from app.sources.akshare_source import AkshareSource  # noqa: E402
from app.sources.ifind import IfindClient  # noqa: E402

# A 股 15:00 收盘。阈值取 15:00 是「判断今天有没有收盘」用的 ——
# 再往后（比如取成 16:00）会把当天判成「还没收盘」，反而把数据写到前一天。
# 注意它**与定时采集时刻无关**：定时采集是 17:30，这里问的是市场收没收盘。
MARKET_CLOSE = time(15, 0)


def _latest_trade_date() -> date:
    """库中最近一个**已经收盘**的交易日。

    两个坑叠在一起，都指向「不能拿今天直接查」：

    1. **周末 / 节假日**没有数据。拿今天去查机构席位，接口只会白跑三次重试。
    2. **交易日但还没到收盘**同样没有数据 —— 凌晨和上午跑，龙虎榜是空的、
       ETF 快照也还是上一交易日的。实测踩过：09-21 凌晨补采，1621 行 ETF
       被写在 09-21 上，而份额其实还是 09-18 的。
       （ETF 那边现在由数据源自带的「数据日期」兜住了，见 `collect_funds.collect_etf`；
       龙虎榜没有这种字段，只能在这里避开。）

    交易日历为空时退回今天（`init_db` 后通常已有，日历由 akshare 提供）。
    """
    today = date.today()
    if datetime.now().time() < MARKET_CLOSE:
        today -= timedelta(days=1)
    with session_scope() as session:
        found = session.scalar(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date <= today)
            .order_by(TradeCalendar.trade_date.desc())
            .limit(1)
        )
    return found or today


def main() -> int:
    parser = argparse.ArgumentParser(description="补采资金面数据")
    parser.add_argument("--days", type=int, default=30, help="回看的日历天数（默认 30）")
    parser.add_argument("--date", help="截止日，默认取最近一个已收盘的交易日")
    parser.add_argument(
        "--skip-etf",
        action="store_true",
        help="跳过 ETF 份额（它只有当日快照，回补历史没有意义）",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    init_db()

    end = date.fromisoformat(args.date) if args.date else _latest_trade_date()
    ifind = IfindClient()
    ak = AkshareSource()

    print(f"截止 {end} · 回看 {args.days} 天")
    # 两融与沪深股通走 iFinD EDB，各花 1 次配额 —— 区间开多大都是 1 次
    print(f"  两融      rows={collect_funds.collect_margin(ifind, end, args.days)}")
    print(f"  沪深股通  rows={collect_funds.collect_hsgt(ifind, end, args.days)}")
    if not args.skip_etf:
        # ETF 与机构席位走 akshare，不占 iFinD 配额。
        # ETF 的日期由数据源决定，与上面的 end 无关
        print(f"  ETF 份额  rows={collect_funds.collect_etf(ak)}")
    print(f"  机构席位  rows={collect_funds.collect_lhb_institutions(ak, end, args.days)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
