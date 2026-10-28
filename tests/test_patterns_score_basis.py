"""形态模式下的「评分」按哪些形态取分（api/patterns._score_basis）。

2026-10-28 用户指出：筛选「明天盯」时整列评分都是 `0.0`。原因是悟道那几个池在
`SCORE_EXCLUDED` 里（不计入默认榜评分），选中它们时 `wanted` 变成空集，于是每行都被
回填 0 —— 评分列看起来像坏了。现在退回「所选形态自身」的分数：这一屏本来就是按
该形态排的，用户点「明天盯」想看的就是这一池里谁高。

注意别把这条当成「悟道池开始计分」：默认视图没有 `picked`，那几个池在默认榜里仍然
记 0 分（见 `hits` 里 `grouped.values()` 那次重算）。
"""

from app.api.patterns import _score_basis
from app.services.patterns import SCORE_EXCLUDED


def test_normal_pattern_uses_selected_only():
    """选了普通形态：只按它取分，不把票上其它形态的分混进来。"""
    assert _score_basis(["volume_surge"]) == {"volume_surge"}


def test_mixed_selection_ignores_excluded_pools():
    """普通形态 + 悟道池混选：仍只看普通形态（悟道池不参与打分）。"""
    assert _score_basis(["volume_surge", "wudao_sample"]) == {"volume_surge"}


def test_only_excluded_pools_falls_back_to_itself():
    """只选悟道池：退回所选形态自身，否则整列会是 0.0。"""
    assert _score_basis(["wudao_sample"]) == {"wudao_sample"}
    assert _score_basis(["wudao_start", "wudao_sample"]) == {
        "wudao_start",
        "wudao_sample",
    }


def test_every_excluded_key_can_still_score():
    """名单将来增加成员时也要能拿到分 —— 单独选中任何一个都不该是空集。"""
    for key in SCORE_EXCLUDED:
        assert _score_basis([key]) == {key}
