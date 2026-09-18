"""Gate 适配器账户权益字段兼容测试。"""

import asyncio
import json
from decimal import Decimal

from exchanges.gate import GateAdapter
from models import Instrument, OrderRequest, PositionSide


def _adapter_with_account(row: dict) -> GateAdapter:
    """绕过需要 API Key 的构造函数，只测试 get_equity 的字段解析。"""
    adapter = GateAdapter.__new__(GateAdapter)

    async def fake_request(*args, **kwargs):
        return row

    adapter._request = fake_request
    return adapter


def test_gate_equity_falls_back_to_available_when_total_is_zero():
    """Gate 测试网可能返回 total=0，但 available/cross_available 是真实权益。"""
    adapter = _adapter_with_account({
        "user": 1,
        "total": "0",
        "available": "1999",
        "cross_available": "1999",
    })
    assert asyncio.run(adapter.get_equity()) == Decimal("1999")


def test_gate_equity_keeps_negative_total():
    """负权益不能被 available 掩盖，必须保留给上层拒绝开仓。"""
    adapter = _adapter_with_account({
        "user": 1,
        "total": "-1.5",
        "available": "100",
        "cross_available": "100",
    })
    assert asyncio.run(adapter.get_equity()) == Decimal("-1.5")


def _protection_adapter() -> tuple[GateAdapter, dict]:
    """构造只拦截 HTTP 请求的适配器，用于核对条件单请求体。"""
    adapter = GateAdapter.__new__(GateAdapter)
    adapter.order_symbols = {}
    captured: dict = {}

    async def fake_request(method, path, params=None, payload=None, private=False):
        captured.update({"method": method, "path": path, "payload": payload,
                         "body": json.dumps(payload, separators=(",", ":"))})
        return {"id": 11259000724489764, "text": payload["initial"]["text"], "status": "open",
                "contract": payload["initial"]["contract"]}

    adapter._request = fake_request
    return adapter, captured


def _eth_request(order_side: str, position_side: PositionSide, quantity: str, price: str) -> OrderRequest:
    # Gate ETH_USDT 一张合约 = 0.01 ETH，与线上 entry 数量 1.62 ETH（=162 张）一致。
    instrument = Instrument("ETH/USDT:PERP", "ETH_USDT", Decimal("0.01"), Decimal("0.01"),
                            Decimal("0.01"), Decimal("0"), Decimal("0.01"))
    return OrderRequest(instrument, position_side, order_side, "MARKET", Decimal(quantity),
                        Decimal(price), True, "ctgate001")


def test_gate_stop_loss_sends_integer_contract_size():
    """Gate 条件单的 initial.size 必须是 JSON 整数，传字符串会被 400 拒绝。"""
    adapter, captured = _protection_adapter()
    result = asyncio.run(adapter.place_stop_loss(
        _eth_request("SELL", PositionSide.LONG, "1.62", "2455")))
    initial = captured["payload"]["initial"]
    assert captured["path"] == "/futures/usdt/price_orders"
    assert isinstance(initial["size"], int) and initial["size"] == -162
    assert '"size":-162' in captured["body"]          # 序列化后必须是数字字面量，不能带引号
    assert captured["payload"]["trigger"]["rule"] == 2  # 多头止损：跌破触发
    assert captured["payload"]["trigger"]["price"] == "2455"
    assert captured["payload"]["trigger"]["expiration"] == 86400
    assert result.exchange_order_id == "11259000724489764"
    assert result.status == "NEW"


def test_gate_take_profit_uses_integer_size_and_long_rule():
    """多头止盈同样要求整数张数，且触发方向与止损相反。"""
    adapter, captured = _protection_adapter()
    asyncio.run(adapter.place_take_profit(_eth_request("SELL", PositionSide.LONG, "1.62", "2489")))
    assert captured["payload"]["initial"]["size"] == -162
    assert captured["payload"]["trigger"]["rule"] == 1
