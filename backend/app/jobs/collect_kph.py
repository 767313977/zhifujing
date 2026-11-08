"""开盘红本地数据服务的**补采**（当前只接市场情绪与涨跌统计）。

## 为什么要它

`market_sentiment` 这张表一直由 iFinD 组装（`DailyCollector.collect_sentiment`）。
本地服务（`StockDataServer.exe`，见 `sources/kph_local.py`）能直接给两个口径：
`get_market_sentiment_once`（情绪分、涨停数、连板高度）与 `get_zd_stat`
（涨/跌家数、涨跌停家数、-10…10 档位分布）。这里把它当**补位源**用：

- **已有值一律不动**：先读库里那一行，只保留「当前为空」的字段再写
  （`upsert_fill` 本身只保证「新的 None 不覆盖」，非空新值会盖掉旧值 ——
  对补位源来说那是不对的：iFinD 是主源，这里只补它没采到的）；
- 日期对不上就整段跳过（上游「问今天给昨天」是常态，见下）；
- 一行都没填到就**不建行** —— 免得造出只有 `trade_date` 的空行。

## 只接这两类的理由（2026-11-08）

说明书里打板池 / 竞价 / 龙虎榜的行是**位置数组**（`100` = 「原始数组，按顺序对应
上游列」），但**没给列顺序**。没有真实响应就写解析器，只能猜列号；猜错不会报错，
只会把成交额写进换手率这种坑里。所以这三类等探针（`scripts/check_kph.py`）拿到
实测字段后再补 —— 与 `sources/kaipanhong.py` 顶部那条「宁可空着，也不填一个猜的数」
同一个取舍。

## ⚠️ 日期必须核对

`payload.data.date` 是这批数据**实际哪一天**的。实时接口在当天早些时候可能返回
**前一天**的数据（涨停天梯就踩过：15:05 问 09-30 给了 09-29 的 56 行，行数正常、
不报错，照 09-30 落库会把昨天的板块安到今天的票上，见 `jobs/collect_themes.py`）。
所以这里**必须** `date == trade_date`，缺失或不等都跳过，绝不「将就着写」。
"""

import argparse
import json
import logging
import sys
from datetime import date

from app.config import Settings, get_settings
from app.db import session_scope, upsert_fill
from app.models import MarketSentiment
from app.sources.kph_local import KphLocalError, KphLocalSource
from app.timeutil import today_cn

logger = logging.getLogger(__name__)

#: 任务名（写日志 / 命令行输出用）
TASK = "kph_sentiment"


def _to_int(value: object) -> int | None:
    """接口里的数字可能是字符串、空串或小数，统一成 int 或 None。"""
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _data_date(data: dict) -> date | None:
    """`payload.data.date` → date；解析不出来给 None（调用方按「对不上」处理）。"""
    raw = str(data.get("date") or "").strip()[:10]
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _short(exc: Exception, limit: int = 160) -> str:
    """异常文案压短：requests 的报错会带上整条 URL 与连接细节，摘要里没必要那么长。"""
    text = str(exc).strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


class KphCollector:
    """把本地服务的情绪 / 涨跌统计补进 `market_sentiment`。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.source = KphLocalSource(self.settings)

    def _fetch(self, trade_date: date, summary: dict) -> tuple[dict, dict]:
        """取两个接口，各自核对日期。返回 `(sentiment, zd_stat)`，取不到的给空 dict。"""
        sentiment: dict = {}
        zd_stat: dict = {}
        try:
            data = self.source.market_sentiment()
            if _data_date(data) == trade_date:
                sentiment = data
                summary["sentiment"] = "ok"
            else:
                summary["sentiment"] = f"日期对不上（{data.get('date')!r}），跳过"
        except (KphLocalError, OSError) as exc:
            summary["sentiment"] = f"失败：{_short(exc)}"
            logger.warning("开盘红市场情绪取数失败：%s", _short(exc))

        try:
            data = self.source.zd_stat()
            if _data_date(data) == trade_date:
                zd_stat = data
                summary["zd_stat"] = "ok"
            else:
                summary["zd_stat"] = f"日期对不上（{data.get('date')!r}），跳过"
        except (KphLocalError, OSError) as exc:
            summary["zd_stat"] = f"失败：{_short(exc)}"
            logger.warning("开盘红涨跌统计取数失败：%s", _short(exc))
        return sentiment, zd_stat

    @staticmethod
    def _row(trade_date: date, sentiment: dict, zd_stat: dict) -> dict:
        """两个来源 → `market_sentiment` 的一行（只放能确定含义的字段）。

        映射依据（说明书 §4.4 的字段说明）：
        - `zt_num` → `limit_up_count`；`zd_stat.zt` 只作为它缺失时的**兜底**；
        - `continue_high` → `max_consecutive`（连板高度）；
        - `dt` → `limit_down_count`；`rise` / `down` → `up_count` / `down_count`。

        `strong`（情绪分）在本表**没有对应列**，这里不写、也不新增列 —— 加字段要考虑
        首页 / 情绪曲线 / 恐惧贪婪的读取口径，不该顺手做。
        """
        row: dict = {"trade_date": trade_date}
        limit_up = _to_int(sentiment.get("zt_num"))
        if limit_up is None:
            limit_up = _to_int(zd_stat.get("zt"))
        row["limit_up_count"] = limit_up
        row["max_consecutive"] = _to_int(sentiment.get("continue_high"))
        row["limit_down_count"] = _to_int(zd_stat.get("dt"))
        row["up_count"] = _to_int(zd_stat.get("rise"))
        row["down_count"] = _to_int(zd_stat.get("down"))
        return row

    def collect(self, trade_date: date, *, dry_run: bool = False) -> dict:
        """采一次。返回一份摘要（CLI 与调度器都直接打这个）。

        两个接口都失败时抛 `KphLocalError` —— 「服务挂了」不能记成功；
        只有一个失败则照常写另一个（`upsert_fill` 不会抹掉已有值）。
        """
        summary: dict = {"task": TASK, "trade_date": trade_date.isoformat(), "written": 0}
        sentiment, zd_stat = self._fetch(trade_date, summary)
        if not sentiment and not zd_stat:
            raise KphLocalError(f"两个接口都没取到可用数据：{summary}")

        row = self._row(trade_date, sentiment, zd_stat)
        filled = {
            key: value for key, value in row.items() if key != "trade_date" and value is not None
        }
        summary["row"] = {key: row[key] for key in row}
        if not filled:
            # 一行都没填到就不写：否则会留下一条只有日期、其余全空的记录，
            # 之后所有「这天采过了」的判断都会把它当成有数据
            summary["reason"] = "没有任何可用字段，未写库"
            return summary
        if dry_run:
            summary["reason"] = "dry-run，未写库"
            return summary

        with session_scope() as session:
            existing = session.get(MarketSentiment, trade_date)
            if existing is not None:
                # 补位语义：库里已经有值的字段一律不动（iFinD 是主源，这里只补空洞）。
                # 不能只靠 `upsert_fill` —— 它只挡「新值 None」，非空新值仍会覆盖旧值。
                row = {
                    key: value
                    for key, value in row.items()
                    if key == "trade_date" or getattr(existing, key) is None
                }
                if len(row) == 1:
                    summary["reason"] = "库里的值已齐，没有要补的字段"
                    return summary
                summary["row"] = {key: row[key] for key in row}
            summary["written"] = upsert_fill(session, MarketSentiment, [row])
        logger.info("开盘红补采 %s：%s", trade_date, summary)
        return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="开盘红本地服务的情绪 / 涨跌统计补采")
    parser.add_argument("--date", default="", help="目标交易日（默认今天，北京时间）")
    parser.add_argument("--dry-run", action="store_true", help="只取数、不写库")
    args = parser.parse_args(argv)

    try:
        target = date.fromisoformat(args.date) if args.date else today_cn()
    except ValueError:
        print(f"日期格式不对：{args.date}（要 yyyy-mm-dd）")
        return 2

    collector = KphCollector()
    try:
        summary = collector.collect(target, dry_run=args.dry_run)
    except KphLocalError as exc:
        print(f"✗ {exc}")
        return 1
    # `default=str`：摘要里的 `row.trade_date` 是 date 对象，不转就抛 TypeError
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
