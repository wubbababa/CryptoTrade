"""验证真实接口拒单详情及客户端编号约束。"""
from types import SimpleNamespace
from models import Exchange
from exchanges.okx import _error_message
from trading_service import TradingService
import asyncio
import pytest
from exchanges.okx import OKXAdapter
from exchanges.base import ExchangeError


@pytest.mark.parametrize("level,position,error", [
    ("1", "net_mode", "现货模式"),
    ("2", "long_short_mode", "持仓模式不匹配"),
    ("2", "net_mode", None),
])
def test_account_validation_is_read_only(level, position, error):
    adapter = object.__new__(OKXAdapter)
    adapter.config = {"position_mode": "ONE_WAY"}
    adapter.mode = "DEMO"

    async def request(method, path):
        assert method == "GET" and path == "/api/v5/account/config"
        return [{"acctLv": level, "posMode": position}]

    adapter._request = request
    if error:
        with pytest.raises(ExchangeError, match=error):
            asyncio.run(adapter.validate_account())
    else:
        asyncio.run(adapter.validate_account())


def test_error_preserves_order_rejection():
    message = _error_message({"code": "1", "msg": "All operations failed",
        "data": [{"sCode": "51000", "sMsg": "Parameter clOrdId error"}]})
    assert "51000" in message
    assert "Parameter clOrdId error" in message


def test_order_id_is_valid_stable_and_distinct():
    command = SimpleNamespace(exchange=Exchange.OKX, command_id="tg--1003795266191-9")
    first = TradingService._client_order_id(command, "ENTRY")
    assert first.isascii() and first.isalnum() and len(first) <= 28
    assert first == TradingService._client_order_id(command, "ENTRY")
    assert first != TradingService._client_order_id(command, "STOP")
    command.command_id = "tg--1003795266191-10"
    assert first != TradingService._client_order_id(command, "ENTRY")


def test_okx_bracket_order_payload():
    """验证 OKXAdapter 下单时正确构建附带止盈止损参数。"""
    from decimal import Decimal
    from models import Instrument, OrderRequest, PositionSide

    adapter = object.__new__(OKXAdapter)
    adapter.config = {"position_mode": "ONE_WAY"}
    adapter.mode = "DEMO"
    adapter._leverage_set = set()
    adapter.order_symbols = {}

    sent_payload = None

    async def mock_validate():
        return None

    async def mock_request(method, path, params=None, payload=None, private=True):
        nonlocal sent_payload
        if path == "/api/v5/public/mark-price":
            return [{"markPx": "60000"}]
        if path == "/api/v5/account/set-leverage":
            return []
        if path == "/api/v5/trade/order":
            sent_payload = payload
            return [{"ordId": "okx-12345", "clOrdId": payload["clOrdId"], "sCode": "0", "sMsg": ""}]
        return []

    adapter.validate_account = mock_validate
    adapter._request = mock_request

    inst = Instrument("BTC/USDT:PERP", "BTC-USDT-SWAP", Decimal("0.1"), Decimal("0.01"), Decimal("0.01"), Decimal("0"), Decimal("1"))
    req = OrderRequest(
        instrument=inst,
        position_side=PositionSide.LONG,
        order_side="BUY",
        order_type="LIMIT",
        quantity=Decimal("1.0"),
        price=Decimal("60000"),
        reduce_only=False,
        client_order_id="ct1234567890abcdef",
        take_profit_price=Decimal("62000"),
        stop_loss_price=Decimal("59000"),
    )

    result = asyncio.run(adapter.place_entry_order(req))
    assert result.exchange_order_id == "okx-12345"
    assert sent_payload is not None
    assert "attachAlgoOrds" in sent_payload
    attach = sent_payload["attachAlgoOrds"][0]
    assert attach["tpTriggerPx"] in {"62000", "62000.0"}
    assert attach["tpOrdPx"] == "-1"
    assert attach["tpTriggerPxType"] == "mark"
    assert attach["slTriggerPx"] in {"59000", "59000.0"}
    assert attach["slOrdPx"] == "-1"
    assert attach["slTriggerPxType"] == "mark"


def test_binance_pending_protection_uses_close_position():
    """未成交进场的 Binance 保护单必须使用全平模式，不能提前传减仓数量。"""
    from decimal import Decimal
    from exchanges.binance import BinanceAdapter
    from models import Instrument, OrderRequest, PositionSide

    adapter = object.__new__(BinanceAdapter)
    adapter.order_symbols = {}
    adapter.algo_orders = set()
    sent_params = None

    async def mock_request(method, path, params=None, private=True, api_key_only=False):
        nonlocal sent_params
        assert method == "POST" and path == "/fapi/v1/algoOrder" and private is True
        sent_params = params
        return {"algoId": "binance-algo-1", "clientAlgoId": params["clientAlgoId"], "algoStatus": "NEW"}

    adapter._request = mock_request
    instrument = Instrument("ETH/USDT:PERP", "ETHUSDT", Decimal("0.01"), Decimal("0.001"), Decimal("0.001"), Decimal("0"))
    request = OrderRequest(instrument, PositionSide.LONG, "SELL", "MARKET", Decimal("1"),
                           Decimal("2500"), True, "ctpending", close_position=True)
    asyncio.run(adapter.place_take_profit(request))
    assert sent_params["closePosition"] == "true"
    assert "quantity" not in sent_params
    assert "reduceOnly" not in sent_params


def test_okx_leverage_is_capped_by_instrument_limit():
    """配置杠杆超过 OKX 合约上限时，自动采用交易所上限。"""
    from decimal import Decimal
    from models import Instrument

    adapter = object.__new__(OKXAdapter)

    async def mock_request(method, path, params=None, payload=None, private=True):
        assert method == "GET"
        assert path == "/api/v5/public/position-tiers"
        assert params["instFamily"] == "XAU-USDT"
        assert "instId" not in params
        return [{"tier": "1", "maxLever": "50"}, {"tier": "2", "maxLever": "20"}]

    adapter._request = mock_request
    inst = Instrument("XAU/USDT:PERP", "XAU-USDT-SWAP", Decimal("0.1"),
                      Decimal("0.01"), Decimal("0.01"), Decimal("0"))
    actual = asyncio.run(adapter.resolve_leverage(inst, Decimal("100"), "CROSS"))
    assert actual == Decimal("50")


def test_okx_rejects_protection_prices_against_mark_price():
    """币种与价格明显错配时，在调用下单接口前给出清晰错误。"""
    from decimal import Decimal
    from models import Instrument, OrderRequest, PositionSide

    adapter = object.__new__(OKXAdapter)

    async def mock_request(method, path, params=None, payload=None, private=True):
        assert path == "/api/v5/public/mark-price"
        return [{"markPx": "3500"}]

    adapter._request = mock_request
    inst = Instrument("XAU/USDT:PERP", "XAU-USDT-SWAP", Decimal("0.1"),
                      Decimal("0.01"), Decimal("0.01"), Decimal("0"))
    req = OrderRequest(inst, PositionSide.LONG, "BUY", "LIMIT", Decimal("1"),
                       Decimal("60000"), False, "ct123", leverage=Decimal("50"),
                       take_profit_price=Decimal("62000"), stop_loss_price=Decimal("59000"))
    with pytest.raises(ExchangeError, match="请检查币种与价格是否对应"):
        asyncio.run(adapter._validate_attached_prices(req))
