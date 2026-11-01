"""`push2his` 默认关闭（2026-11-01 审计 P2-5）。

README 明确「只碰 push2ex，不碰 push2* 集群」；这条线默认不再发请求，
调用方自然落到下一级（腾讯）。要临时排查才显式设 `ENABLE_PUSH2HIS=1`。
"""

from app.sources import eastmoney


def test_push2his_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_PUSH2HIS", raising=False)
    assert eastmoney.push2his_enabled() is False
    # 关闭时**不发请求**、直接空列表（上层按 `_NoBars` 走下一级）
    assert eastmoney.fetch_daily("600000", days=10) == []


def test_push2his_env_toggle(monkeypatch):
    for value in ("1", "true", "YES"):
        monkeypatch.setenv("ENABLE_PUSH2HIS", value)
        assert eastmoney.push2his_enabled() is True
    monkeypatch.setenv("ENABLE_PUSH2HIS", "0")
    assert eastmoney.push2his_enabled() is False
