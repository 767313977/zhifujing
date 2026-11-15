"""Trend Engine V1.0：趋势启动 / 趋势延续双模型。

本包只提供**纯计算**（指标、结构判定、评分、解释、风险状态），不碰数据库、不写库。
数据读取由调用方（API / 回测脚本）完成，保证同一份判据在线上与回测里逐字一致。

版本约定：任何阈值或口径变化都必须升 `CALC_VERSION` / 模型版本，历史快照不得无痕覆盖。
"""

from app.services.trend_engine import config

CALC_VERSION = config.CALC_VERSION

__all__ = ["CALC_VERSION"]
