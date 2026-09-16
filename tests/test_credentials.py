"""交易环境凭据隔离测试。"""

import pytest

from exchanges.credentials import credentials


def test_demo_and_live_credentials_are_selected_independently(monkeypatch):
    """同一进程存在两套 Key 时，必须只读取当前模式对应的一套。"""
    monkeypatch.setenv("OKX_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("OKX_LIVE_API_KEY", "live-key")
    assert credentials("OKX", "DEMO", "API_KEY") == ("demo-key",)
    assert credentials("OKX", "LIVE", "API_KEY") == ("live-key",)


def test_environmentless_legacy_key_is_not_used(monkeypatch):
    """拒绝旧变量，避免 mode 切换后误把另一环境的 Key 用于实盘。"""
    # 隔离开发机 .env 中的正式凭据，确保本测试只验证旧变量不会被回退使用。
    monkeypatch.delenv("BINANCE_LIVE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_LIVE_API_SECRET", raising=False)
    monkeypatch.setenv("BINANCE_API_KEY", "legacy-key")
    with pytest.raises(ValueError, match="BINANCE_LIVE_API_KEY"):
        credentials("BINANCE", "LIVE", "API_KEY")
