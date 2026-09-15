"""开发者测试模式 (dev_runner) 单元测试。"""

import json
from unittest.mock import patch

import pytest

from dev_runner import run_dev_test
from settings import Settings
from tests.test_deepseek_parser import MockResponse, MockSession


@pytest.fixture
def dev_settings_file(tmp_path):
    config_file = tmp_path / "config.yaml"
    source = Settings.load("config.yaml").raw
    source["deepseek"] = {
        "api_key": "test-mock-dev-key",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "timeout_seconds": 15,
        "minimum_confidence": "0.90",
    }
    source["database_path"] = str(tmp_path / "dev_test.db")
    for exchange_config in source["exchanges"].values():
        exchange_config["mode"] = "LOCAL"
    import yaml
    config_file.write_text(yaml.safe_dump(source, allow_unicode=True), encoding="utf-8")
    return str(config_file)


@pytest.mark.asyncio
async def test_run_dev_test_end_to_end(dev_settings_file, capsys):
    fake_response_data = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "command_type": "OPEN_POSITION",
                        "exchange": "OKX",
                        "base_asset": "ETH",
                        "side": "LONG",
                        "entry": {"type": "RANGE", "low": "2480", "high": "2500"},
                        "take_profits": ["2550", "2600"],
                        "stop_loss": "2420",
                        "quantity": None,
                        "confidence": "0.98",
                        "ambiguities": [],
                    })
                }
            }
        ]
    }

    mock_resp = MockResponse(200, fake_response_data)
    mock_session = MockSession(mock_resp)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        await run_dev_test(config_path=dev_settings_file)

    captured = capsys.readouterr().out
    assert "启动 CryptoTrade 开发者测试模式 (--dev)" in captured
    assert "DeepSeek API 原始返回 JSON 内容" in captured
    assert "TradeCommand 实体" in captured
    assert "已提交" in captured
    assert "开发者测试模式运行完毕" in captured
