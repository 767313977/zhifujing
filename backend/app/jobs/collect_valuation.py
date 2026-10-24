"""指数估值采集（零 iFinD 配额）：蛋卷周频历史 → `index_valuation`。

每天整段重拉一遍（8 个指数 × PE/PB 两个端点 = 16 次 HTTP，约几秒），
而不是只补最新一天 —— 来源会**回改最近几周**的采样值（指数成分调整、财报口径修订），
整段 upsert 才能把订正带进来；这一点与 `collect_erp` 的「重算最近 30 天」同理。

失败处理：**单个指数失败只跳过它自己**（记 warning），全部失败才抛错。
估值是增强读数，不该因为某个指数改名就把整条尾部链记成失败。
"""

import logging

from app.db import session_scope, upsert_many
from app.models import IndexValuation
from app.services.valuation import CORE_INDICES
from app.sources import danjuan

logger = logging.getLogger(__name__)

#: 数据来源标识，写进 `index_valuation.source`，排查口径时用
SOURCE = "danjuan"


def refresh() -> int:
    """重拉全部核心指数的估值序列，返回写入行数。"""
    records: list[dict] = []
    failed: list[str] = []
    for code, name, danjuan_code in CORE_INDICES:
        try:
            pe_points = danjuan.history(danjuan_code, "pe")
            pb_by_day = {
                point.day: point.value
                for point in danjuan.history(danjuan_code, "pb")
            }
        except danjuan.DanjuanError as exc:
            logger.warning("指数估值取数失败（%s）：%s", name, exc)
            failed.append(name)
            continue

        kept = 0
        for point in pe_points:
            pb = pb_by_day.get(point.day)
            # PE、PB 共用同一套采样网格；只有一边的点跳过不写 —— 写半行会让
            # 「PB 分位」在缺值的日子变成 0 或断档，看起来像真的到过 0 倍市净率
            if pb is None:
                continue
            records.append(
                {
                    "trade_date": point.day,
                    "index_code": code,
                    "name": name,
                    "pe_ttm": point.value,
                    "pb": pb,
                    "source": SOURCE,
                }
            )
            kept += 1
        logger.info("指数估值 %s：%d 个采样点", name, kept)

    if not records:
        raise RuntimeError(f"指数估值取到 0 行（{len(failed)} 个指数全部失败）")

    with session_scope() as session:
        written = upsert_many(session, IndexValuation, records)
    logger.info("指数估值写入 %d 行（失败 %d 个指数）", written, len(failed))
    return written
