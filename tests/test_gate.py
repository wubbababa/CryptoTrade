"""Gate 适配器账户权益字段兼容测试。"""

import asyncio
from decimal import Decimal

from exchanges.gate import GateAdapter


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
