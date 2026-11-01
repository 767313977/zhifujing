"""P3 小修的两条回归：用户名控制字符、ETF 分类中位数口径。"""

import pytest
from fastapi import HTTPException

from app.api import auth as auth_api
from app.services.etf_screen import _median


def test_username_rejects_control_characters():
    with pytest.raises(HTTPException) as exc:
        auth_api._normalize_username("bad\nname")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):
        auth_api._normalize_username("bad\x00name")
    # 正常中文 / 字母数字照常通过
    assert auth_api._normalize_username("  张三abc  ") == "张三abc"


def test_median_matches_standard_definition():
    assert _median([]) is None
    assert _median([3.0]) == 3.0
    assert _median([1.0, 3.0]) == 2.0  # 旧口径会得到 3.0（上中位）
    assert _median([1.0, 2.0, 3.0]) == 2.0
    assert _median([1.0, 2.0, 3.0, 4.0]) == 2.5
