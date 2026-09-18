"""Monitor 推送解析的健壮性测试。

回归用例：Gate 的订阅回执把 `result` 写成对象而不是列表，早期实现直接 `payload[0]`
会抛 `KeyError(0)`（线上日志表现为「GATE 事件流断开，实时策略已暂停：0」），
并让该交易所的事件流任务退出、自动保本永久失效。

回归用例：OKX/Gate 的成交推送以「合约张数」回报成交量，早期实现直接入库，本地数量因此
比远程仓位大若干倍（线上日志表现为 ETH/USDT:PERP 远程仓位=77.54，本地已关联交易数量
=775.4），汇总仓位自动保本被永久拒绝。解析层只做归一，换算统一由
`Monitor._parse_order_event` 通过适配器的合约面值倍率完成。
"""

import asyncio
from decimal import Decimal

from exchanges.base import PaperAdapter
from models import Exchange, Instrument
from monitor import Monitor, _first_row


def test_first_row_normalizes_list_and_object_payloads():
    assert _first_row([{"contract": "BTC_USDT"}]) == {"contract": "BTC_USDT"}
    # 订阅回执：对象形状，取字段前必须归一为 dict，不能当作列表索引。
    assert _first_row({"status": "success"}) == {"status": "success"}
    assert _first_row([]) == {}
    assert _first_row(None) == {}
    assert _first_row("unexpected") == {}


def test_gate_subscribe_acks_do_not_raise():
    """Gate 订阅回执必须安全返回 None，而不是抛 KeyError(0)。"""
    ticker_ack = {"time": 1, "channel": "futures.tickers", "event": "subscribe",
                  "result": {"status": "success"}, "error": None}
    order_ack = {"time": 1, "channel": "futures.orders", "event": "subscribe",
                 "result": {"status": "success"}, "error": None}

    assert Monitor._extract_market_event("GATE", ticker_ack) is None
    # 订单回执不携带客户编号，返回的是空编号事件，交给后续查库自然忽略。
    assert Monitor._extract_order_event("GATE", order_ack).client_order_id == ""


def test_gate_real_updates_are_still_parsed():
    """形状归一不能影响真实推送的解析。"""
    ticker = {"time": 1, "channel": "futures.tickers", "event": "update",
              "result": [{"contract": "BTC_USDT", "mark_price": "60000"}]}
    assert Monitor._extract_market_event("GATE", ticker) == ("BTC/USDT:PERP", 60000)

    order = {"channel": "futures.orders", "event": "update",
             "result": [{"text": "t-ctabc", "status": "open", "size": "-3", "left": "-1",
                         "contract": "BTC_USDT"}]}
    event = Monitor._extract_order_event("GATE", order)
    assert event.client_order_id == "ctabc"
    assert event.status == "OPEN"
    # 解析层保留交易所原始计量单位（Gate 为合约张数），换算交给 _parse_order_event。
    assert str(event.filled_quantity) == "2"
    assert event.symbol == "BTC_USDT"


def _contract_adapter(multiplier: Decimal, symbol: str) -> PaperAdapter:
    """构造「以合约张数成交」的适配器，用于验证成交量单位换算。"""
    return PaperAdapter({
        "ETH": Instrument("ETH/USDT:PERP", symbol, Decimal("0.01"), Decimal("0.1"),
                          Decimal("0.1"), Decimal("0"), multiplier),
    }, Decimal("1000"))


def test_okx_order_event_fill_is_converted_from_contracts_to_base_quantity():
    """OKX 合约张数必须换算成标的数量后再落库，否则本地数量比远程仓位多 10 倍。"""
    event = {"arg": {"channel": "orders"}, "data": [{
        "clOrdId": "ct1", "state": "filled", "accFillSz": "775.4", "avgPx": "2477",
        "instId": "ETH-USDT-SWAP",
    }]}
    parsed = asyncio.run(Monitor(None, None)._parse_order_event(
        Exchange.OKX, _contract_adapter(Decimal("0.1"), "ETH-USDT-SWAP"), event,
    ))
    assert parsed.filled_quantity == Decimal("77.54")
    # 价格本身按标的计价，换算成交量时不得改动成交均价。
    assert parsed.average_price == Decimal("2477")


def test_gate_order_event_fill_is_converted_from_contracts_to_base_quantity():
    """Gate 同样以合约张数回报成交量，必须按 quanto_multiplier 换算。"""
    event = {"channel": "futures.orders", "event": "update", "result": [{
        "text": "t-ct2", "status": "finished", "finish_as": "filled",
        "size": "3", "left": "1", "fill_price": "60000", "contract": "BTC_USDT",
    }]}
    parsed = asyncio.run(Monitor(None, None)._parse_order_event(
        Exchange.GATE, _contract_adapter(Decimal("0.0001"), "BTC_USDT"), event,
    ))
    assert parsed.status == "FILLED"
    assert parsed.filled_quantity == Decimal("0.0002")


def test_okx_subscribe_ack_does_not_raise():
    """OKX 回执没有 data 字段，也必须安全返回 None。"""
    ack = {"event": "subscribe", "arg": {"channel": "tickers", "instId": "BTC-USDT-SWAP"}}
    assert Monitor._extract_market_event("OKX", ack) is None
    assert Monitor._extract_order_event("OKX", ack) is None
