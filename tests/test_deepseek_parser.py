"""DeepSeek API 公告解析器单元测试。"""

import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from deepseek_parser import DeepSeekParser, ParseError, _extract_json, command_from_json
from models import CommandType, Exchange, PositionSide
from settings import Settings


@pytest.fixture
def test_settings(tmp_path):
    config = tmp_path / "config.yaml"
    source = Settings.load("config.yaml").raw
    source["deepseek"] = {
        "api_key": "test-deepseek-api-key",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "timeout_seconds": 15,
        "minimum_confidence": "0.90",
    }
    for exchange_config in source["exchanges"].values():
        exchange_config["mode"] = "LOCAL"
    import yaml
    config.write_text(yaml.safe_dump(source, allow_unicode=True), encoding="utf-8")
    return Settings.load(config)


def test_extract_json_clean_object():
    raw = '{"command_type": "OPEN_POSITION", "base_asset": "BTC"}'
    assert _extract_json(raw)["command_type"] == "OPEN_POSITION"


def test_extract_json_markdown_code_block():
    raw = """这是一段分析：
```json
{
  "command_type": "OPEN_POSITION",
  "base_asset": "ETH",
  "exchange": "BINANCE"
}
```
请查收。"""
    res = _extract_json(raw)
    assert res["command_type"] == "OPEN_POSITION"
    assert res["base_asset"] == "ETH"


def test_extract_json_nested_structure():
    raw = 'prefix text {"command_type": "OPEN_POSITION", "breakeven": {"trigger_ratio": "0.5"}} suffix'
    res = _extract_json(raw)
    assert res["command_type"] == "OPEN_POSITION"
    assert res["breakeven"]["trigger_ratio"] == "0.5"


def test_command_from_json_mapping(test_settings):
    payload = {
        "command_type": "OPEN_POSITION",
        "exchange": "OKX",
        "base_asset": "SOL",
        "side": "LONG",
        "entry": {"type": "RANGE", "low": "145.5", "high": "146.0"},
        "take_profits": ["150", "155"],
        "stop_loss": "142",
        "quantity": "100",  # 将被强制覆盖为 None
        "confidence": "0.95",
        "ambiguities": [],
        "breakeven": {"trigger_ratio": "0.50", "profit_price_ratio": "0.01"},
    }
    cmd = command_from_json(payload, "tg-100", test_settings)
    assert cmd.command_type == CommandType.OPEN_POSITION
    assert cmd.exchange == Exchange.OKX
    assert cmd.base_asset == "SOL"
    assert cmd.side == PositionSide.LONG
    assert cmd.entry is not None
    assert cmd.entry.low == Decimal("145.5")
    assert cmd.entry.high == Decimal("146.0")
    assert cmd.entry.reference_price == Decimal("145.75")
    assert cmd.quantity is None
    assert cmd.confidence == Decimal("0.95")
    assert cmd.exchange_defaulted is False


def test_command_from_json_default_exchange(test_settings):
    payload = {
        "command_type": "OPEN_POSITION",
        "exchange": None,
        "base_asset": "BTC",
        "side": "SHORT",
        "entry": {"type": "RANGE", "low": "60000", "high": "60500"},
        "take_profits": ["58000"],
        "stop_loss": "61000",
        "confidence": "0.99",
    }
    cmd = command_from_json(payload, "tg-101", test_settings)
    assert cmd.exchange_defaulted is True
    assert cmd.exchange == test_settings.default_exchange


class MockResponse:
    def __init__(self, status: int, data: dict):
        self.status = status
        self._data = data

    async def json(self) -> dict:
        return self._data

    async def text(self) -> str:
        return json.dumps(self._data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class MockSession:
    def __init__(self, response: MockResponse):
        self._response = response

    def post(self, *args, **kwargs):
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.mark.asyncio
async def test_deepseek_parser_successful_api_mock(test_settings):
    parser = DeepSeekParser(test_settings)
    fake_response_data = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "command_type": "OPEN_POSITION",
                        "exchange": "BINANCE",
                        "base_asset": "ETH",
                        "side": "LONG",
                        "entry": {"type": "RANGE", "low": "2500", "high": "2510"},
                        "take_profits": ["2550", "2600"],
                        "stop_loss": "2450",
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
        cmd = await parser.parse("做多 ETH 2500-2510 止损 2450 止盈 2550, 2600", "tg-mock-1")
        assert cmd.base_asset == "ETH"
        assert cmd.side == PositionSide.LONG
        assert cmd.exchange == Exchange.BINANCE
        assert cmd.confidence == Decimal("0.98")


@pytest.mark.asyncio
async def test_deepseek_parser_api_error_handling(test_settings):
    parser = DeepSeekParser(test_settings)
    mock_resp = MockResponse(401, {"error": "Invalid API key"})
    mock_session = MockSession(mock_resp)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(ParseError, match="DeepSeek API 请求失败"):
            await parser.parse("做多 BTC 60000", "tg-mock-err")


@pytest.mark.asyncio
async def test_deepseek_parser_range_entry_prefers_first_price(test_settings):
    """验证范围进场价（如 60000 - 60500）按规则解析为以前者（60000）为主。"""
    parser = DeepSeekParser(test_settings)
    fake_response_data = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "command_type": "OPEN_POSITION",
                        "exchange": "OKX",
                        "base_asset": "BTC",
                        "side": "LONG",
                        "entry": {"type": "RANGE", "low": "60000", "high": "60000"},
                        "take_profits": ["62000", "63000"],
                        "stop_loss": "59000",
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
        cmd = await parser.parse("做多 BTC 60000 - 60500 止损 59000 止盈 62000, 63000", "tg-mock-range")
        assert cmd.base_asset == "BTC"
        assert cmd.side == PositionSide.LONG
        assert cmd.entry is not None
        assert cmd.entry.low == Decimal("60000")
        assert cmd.entry.high == Decimal("60000")
        assert cmd.entry.reference_price == Decimal("60000")
        assert cmd.take_profits == (Decimal("62000"), Decimal("63000"))
        assert cmd.stop_loss == Decimal("59000")


@pytest.mark.asyncio
async def test_deepseek_parser_chinese_asset_gold_normalized(test_settings):
    """验证模型即使返回中文'黄金'，也会被自动标准化为'XAU'并顺利通过白名单。"""
    parser = DeepSeekParser(test_settings)
    fake_response_data = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "command_type": "OPEN_POSITION",
                        "exchange": "OKX",
                        "base_asset": "黄金",
                        "side": "LONG",
                        "entry": {"type": "RANGE", "low": "2500", "high": "2500"},
                        "take_profits": ["2550"],
                        "stop_loss": "2450",
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
        cmd = await parser.parse("做多 黄金 2500 止损 2450 止盈 2550", "tg-mock-gold")
        assert cmd.base_asset == "XAU"


