"""研究缓存身份：缓存键必须包含数据库、代码、参数与数据版本。

M6.2 遗留问题：特征缓存只有一个文件名（`attribution.json`），无法证明缓存对应哪份数据/代码。
本模块提供纯函数生成确定性缓存键，供未来的特征缓存使用；不改变现有缓存文件。
"""

from __future__ import annotations

import hashlib
import json


class CacheIdentityError(ValueError):
    """缓存身份不完整时抛出。"""


def build_cache_key(
    *,
    db_sha256: str,
    code_sha: str,
    params: dict,
    data_version: str,
    extra: dict | None = None,
) -> str:
    """生成确定性缓存键。

    四个必填维度任一为空 / 非法 → 拒绝，避免「只按模型名缓存」的旧问题。
    """
    for name, value in (("db_sha256", db_sha256), ("code_sha", code_sha), ("data_version", data_version)):
        if not isinstance(value, str) or not value.strip():
            raise CacheIdentityError(f"{name} 不能为空")
    if not isinstance(params, dict) or not params:
        raise CacheIdentityError("params 必须是非空字典")

    payload = {
        "db_sha256": db_sha256.strip().upper(),
        "code_sha": code_sha.strip().lower(),
        "params": params,
        "data_version": data_version.strip(),
        "extra": extra or {},
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"m63:{digest}"
