"""Monitor 推送解析的健壮性测试。

回归用例：Gate 的订阅回执把 `result` 写成对象而不是列表，早期实现直接 `payload[0]`
会抛 `KeyError(0)`（线上日志表现为「GATE 事件流断开，实时策略已暂停：0」），
并让该交易所的事件流任务退出、自动保本永久失效。
"""

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
    assert Monitor._extract_order_event("GATE", order_ack)[0] == ""


def test_gate_real_updates_are_still_parsed():
    """形状归一不能影响真实推送的解析。"""
    ticker = {"time": 1, "channel": "futures.tickers", "event": "update",
              "result": [{"contract": "BTC_USDT", "mark_price": "60000"}]}
    assert Monitor._extract_market_event("GATE", ticker) == ("BTC/USDT:PERP", 60000)

    order = {"channel": "futures.orders", "event": "update",
             "result": [{"text": "t-ctabc", "status": "open", "size": "-3", "left": "-1"}]}
    client_order_id, status, filled, _ = Monitor._extract_order_event("GATE", order)
    assert client_order_id == "ctabc"
    assert status == "OPEN"
    assert str(filled) == "2"


def test_okx_subscribe_ack_does_not_raise():
    """OKX 回执没有 data 字段，也必须安全返回 None。"""
    ack = {"event": "subscribe", "arg": {"channel": "tickers", "instId": "BTC-USDT-SWAP"}}
    assert Monitor._extract_market_event("OKX", ack) is None
    assert Monitor._extract_order_event("OKX", ack) is None
