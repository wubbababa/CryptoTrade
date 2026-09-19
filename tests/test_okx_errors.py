"""验证真实接口拒单详情及客户端编号约束。"""
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from database import Database
from settings import Settings
from exchange_router import ExchangeRouter
from codex_parser import command_from_json
from monitor import Monitor
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


def test_okx_entry_order_does_not_attach_protection():
    """P0-2 回归：OKX 进场单不得注入 attachAlgoOrds，保护单改由成交后分别补建。

    旧实现把止盈止损放进 attachAlgoOrds，OKX 会生成一个止盈止损共享 algoId 的 OCO
    算法单，既无法支持「取消止损但保留止盈」，也不会出现在 orders-pending /
    orders-algo-pending 中，导致本地 orders 表没有保护单记录，改止损/改止盈/
    取消止损/保本离场/自动保本在 OKX 上全部不可用。
    """
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
    # 关键断言：进场单只带自身参数，不再携带任何附带保护。
    assert "attachAlgoOrds" not in sent_payload
    assert sent_payload["clOrdId"] == "ct1234567890abcdef"
    assert sent_payload["ordType"] == "limit"
    assert sent_payload["px"] in {"60000", "60000.0"}


def test_okx_adapter_declares_deferred_protection():
    """P0-2 回归：OKX 必须声明为「成交后补建保护单」，否则 Monitor 不会登记保护单。"""
    adapter = object.__new__(OKXAdapter)
    assert adapter.entry_protection_attached is False


def test_okx_conditional_order_sends_algo_client_id():
    """P0-2 回归：算法保护单必须回传 algoClOrdId，否则本地无法与远程挂单唯一关联。"""
    from decimal import Decimal
    from models import Instrument, OrderRequest, PositionSide

    adapter = object.__new__(OKXAdapter)
    adapter.order_symbols = {}
    adapter.algo_orders = set()

    sent = None

    async def mock_request(method, path, params=None, payload=None, private=True):
        nonlocal sent
        assert method == "POST" and path == "/api/v5/trade/order-algo"
        sent = payload
        return [{"algoId": "okx-algo-1", "algoClOrdId": payload["algoClOrdId"], "sCode": "0", "sMsg": ""}]

    adapter._request = mock_request
    inst = Instrument("ETH/USDT:PERP", "ETH-USDT-SWAP", Decimal("0.1"),
                      Decimal("0.01"), Decimal("0.01"), Decimal("0"))
    req = OrderRequest(inst, PositionSide.LONG, "SELL", "MARKET", Decimal("1"),
                       Decimal("2500"), True, "ctProtection01")
    result = asyncio.run(adapter.place_stop_loss(req))
    assert sent is not None
    assert sent["algoClOrdId"] == "ctProtection01"
    assert sent["slTriggerPx"] in {"2500", "2500.0"}
    assert sent["reduceOnly"] == "true"
    # 该编号必须被登记为算法单，撤销时才能路由到 cancel-algos。
    assert "okx-algo-1" in adapter.algo_orders
    assert result.client_order_id == "ctProtection01"


def test_okx_open_orders_include_algo_orders():
    """P0-2 回归：get_open_orders 必须合并 orders-algo-pending，否则保护单在本地不可见。"""
    from decimal import Decimal

    adapter = object.__new__(OKXAdapter)
    adapter.order_symbols = {}
    adapter.algo_orders = set()

    async def mock_request(method, path, params=None, payload=None, private=True):
        assert method == "GET"
        if path == "/api/v5/trade/orders-pending":
            return [{"ordId": "okx-entry-1", "instId": "ETH-USDT-SWAP", "clOrdId": "ctEntry01", "state": "live"}]
        if path == "/api/v5/trade/orders-algo-pending":
            return [{"algoId": "okx-algo-1", "instId": "ETH-USDT-SWAP",
                     "algoClOrdId": "ctProtection01", "state": "live"}]
        raise AssertionError("意外的请求路径：" + path)

    adapter._request = mock_request
    orders = asyncio.run(adapter.get_open_orders())
    assert [o.client_order_id for o in orders] == ["ctEntry01", "ctProtection01"]
    assert [o.status for o in orders] == ["NEW", "NEW"]
    assert "okx-algo-1" in adapter.algo_orders


def test_okx_cancel_routes_algo_orders_to_cancel_algos():
    """P0-2 回归：算法保护单必须走 cancel-algos，普通挂单仍走 cancel-order。"""
    adapter = object.__new__(OKXAdapter)
    adapter.order_symbols = {"okx-algo-1": "ETH-USDT-SWAP", "okx-entry-1": "ETH-USDT-SWAP"}
    adapter.algo_orders = {"okx-algo-1"}

    calls = []

    async def mock_request(method, path, params=None, payload=None, private=True):
        calls.append((path, payload))
        if path == "/api/v5/trade/cancel-algos":
            return [{"algoId": "okx-algo-1", "sCode": "0", "sMsg": ""}]
        return [{"ordId": "okx-entry-1", "sCode": "0", "sMsg": ""}]

    adapter._request = mock_request
    asyncio.run(adapter.cancel_order("okx-algo-1"))
    asyncio.run(adapter.cancel_order("okx-entry-1"))
    assert calls[0][0] == "/api/v5/trade/cancel-algos"
    assert calls[0][1] == [{"instId": "ETH-USDT-SWAP", "algoId": "okx-algo-1"}]
    assert calls[1][0] == "/api/v5/trade/cancel-order"
    assert calls[1][1] == {"instId": "ETH-USDT-SWAP", "ordId": "okx-entry-1"}


def test_binance_protection_uses_quantity_reduce_only():
    """Binance 保护单必须用「数量 + reduceOnly」，不得使用会被 -4509 拒绝的 closePosition。"""
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
                           Decimal("2500"), True, "ctpending")
    asyncio.run(adapter.place_take_profit(request))
    assert sent_params["quantity"] == "1"
    assert sent_params["reduceOnly"] == "true"
    # closePosition 全平语义已被移除：它在未持仓时会被服务端拒绝。
    assert "closePosition" not in sent_params


def test_binance_protection_rejects_non_reduce_only():
    """缺少 reduceOnly 的保护单必须在本地就被拒绝，不能带着错误参数到交易所。"""
    from decimal import Decimal
    from exchanges.base import ExchangeError
    from exchanges.binance import BinanceAdapter
    from models import Instrument, OrderRequest, PositionSide

    adapter = object.__new__(BinanceAdapter)
    adapter.order_symbols = {}
    adapter.algo_orders = set()

    async def mock_request(*args, **kwargs):
        raise AssertionError("参数不合法时不应发起网络请求")

    adapter._request = mock_request
    instrument = Instrument("ETH/USDT:PERP", "ETHUSDT", Decimal("0.01"), Decimal("0.001"), Decimal("0.001"), Decimal("0"))
    request = OrderRequest(instrument, PositionSide.LONG, "SELL", "MARKET", Decimal("1"),
                           Decimal("2500"), False, "ctbad")
    with pytest.raises(ExchangeError, match="只减仓"):
        asyncio.run(adapter.place_stop_loss(request))


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


@pytest.fixture
def settings(tmp_path):
    """与本项目其它用例一致：复用主配置但把交易所切到 LOCAL，避免真实网络访问。"""
    import yaml

    config = tmp_path / "config.yaml"
    source = Settings.load("config.yaml").raw
    for exchange_config in source["exchanges"].values():
        exchange_config["mode"] = "LOCAL"
    config.write_text(yaml.safe_dump(source, allow_unicode=True), encoding="utf-8")
    return Settings.load(config)


def valid_payload():
    """与 tests/test_core.py 相同的基准多头信号，便于本文件独立构造指令。"""
    return {
        "command_type": "OPEN_POSITION", "exchange": "OKX", "base_asset": "ETH",
        "side": "LONG", "entry": {"type": "RANGE", "low": "2480", "high": "2490"},
        "take_profits": ["2519", "2549"], "stop_loss": "2455", "quantity": "0.01",
        "confidence": "0.98", "ambiguities": [],
    }


class FakeOKXTransport:
    """最小可用的 OKX HTTP 仿真：只实现本项目实际调用的接口。

    关键点是复刻两个真实行为：
    1. order-algo 创建的保护单带 algoId，且只在 orders-algo-pending 中出现；
    2. 普通订单只在 orders-pending 中出现，并且不会出现在算法单列表里。
    """

    def __init__(self):
        self.regular = {}          # ordId -> row
        self.algos = {}            # algoId -> row
        self._seq = 0
        self.canceled_algos = []

    def _next(self, prefix):
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def mark_filled(self, ord_id):
        """模拟成交：订单立即离开 orders-pending（OKX 真实行为）。"""
        self.regular.pop(ord_id, None)

    async def __call__(self, method, path, params=None, payload=None, private=True):
        params, payload = params or {}, payload or {}
        if path == "/api/v5/account/config":
            return [{"acctLv": "2", "posMode": "net_mode"}]
        if path == "/api/v5/public/instruments":
            return [{"instId": "ETH-USDT-SWAP", "state": "live", "ctVal": "0.1",
                     "tickSz": "0.01", "lotSz": "1", "minSz": "1"}]
        if path == "/api/v5/public/position-tiers":
            return [{"maxLever": "100"}]
        if path == "/api/v5/public/mark-price":
            return [{"markPx": "2500"}]
        if path == "/api/v5/account/balance":
            return [{"totalEq": "10000"}]
        if path == "/api/v5/account/set-leverage":
            return [{}]
        if path == "/api/v5/account/positions":
            return [{"instId": "ETH-USDT-SWAP", "posSide": "net", "pos": str(self.position_contracts),
                     "avgPx": "2480"}] if self.position_contracts else []
        if path == "/api/v5/trade/order":
            oid = self._next("okx-entry")
            self.regular[oid] = {"ordId": oid, "instId": payload["instId"],
                                 "clOrdId": payload["clOrdId"], "state": "live"}
            return [{"ordId": oid, "clOrdId": payload["clOrdId"], "sCode": "0", "sMsg": ""}]
        if path == "/api/v5/trade/order-algo":
            aid = self._next("okx-algo")
            self.algos[aid] = {"algoId": aid, "instId": payload["instId"],
                               "algoClOrdId": payload["algoClOrdId"], "state": "live"}
            return [{"algoId": aid, "algoClOrdId": payload["algoClOrdId"], "sCode": "0", "sMsg": ""}]
        if path == "/api/v5/trade/orders-pending":
            return list(self.regular.values())
        if path == "/api/v5/trade/orders-algo-pending":
            return list(self.algos.values())
        if path == "/api/v5/trade/cancel-order":
            self.regular.pop(payload["ordId"], None)
            return [{"ordId": payload["ordId"], "sCode": "0", "sMsg": ""}]
        if path == "/api/v5/trade/cancel-algos":
            for item in payload:
                self.algos.pop(item["algoId"], None)
                self.canceled_algos.append(item["algoId"])
            return [{"sCode": "0", "sMsg": ""}]
        if path == "/api/v5/trade/amend-order":
            return [{"ordId": payload["ordId"], "sCode": "0", "sMsg": ""}]
        raise AssertionError("未实现的 OKX 接口：" + path)


def _okx_application(settings):
    """装配一个使用真实 OKXAdapter（mock HTTP）的应用上下文。"""
    database = Database(settings.database_path)
    database.initialize()
    router = ExchangeRouter(settings)
    # 真实 OKXAdapter 的构造函数会读取凭据；这里注入占位值，HTTP 全程由 FakeOKXTransport 接管，
    # 不会发起任何真实请求，因此不需要真实 API Key。
    monkey = pytest.MonkeyPatch()
    for name in ("API_KEY", "API_SECRET", "API_PASSPHRASE"):
        monkey.setenv(f"OKX_LOCAL_{name}", "test-placeholder")
    adapter = OKXAdapter(settings.exchange_config(Exchange.OKX))
    transport = FakeOKXTransport()
    transport.position_contracts = 0
    adapter._request = transport
    adapter.validate_account = adapter.validate_account
    router.adapters[Exchange.OKX] = adapter
    return database, router, adapter, transport


def test_okx_protection_orders_are_registered_and_manageable(settings):
    """P0-2 回归：OKX 成交后必须登记保护单，且改止损/改止盈/取消止损均可用。

    旧实现下 OKX 的 entry_protection_attached=True，_ensure_protection 直接返回，
    orders 表永远没有 TAKE_PROFIT/STOP_LOSS 行，因此上述人工指令全部报
    「未找到活动 STOP_LOSS/TAKE_PROFIT 本地订单」。
    """
    async def scenario():
        database, router, adapter, transport = _okx_application(settings)
        service = TradingService(settings, database, router)
        command = command_from_json(valid_payload(), "tg-okx-p02", settings)
        # 用 OKX 资产/交易所重建指令。
        command = replace(command, exchange=Exchange.OKX, base_asset="ETH")
        await service.execute(command)
        entry = (await database.fetch_all("SELECT * FROM orders WHERE order_type='ENTRY'"))[0]

        # 成交：OKX 以合约张数回报成交量，先按合约面值换算。
        contracts = Decimal(entry["quantity"]) / Decimal("0.1")
        transport.position_contracts = contracts
        # 成交后进场单不再是挂单（否则对账会误报本地/远端挂单不一致）。
        transport.mark_filled(entry["exchange_order_id"])
        monitor = Monitor(router, database)
        await monitor.process_event(Exchange.OKX, adapter, {"arg": {"channel": "orders"}, "data": [{
            "clOrdId": entry["client_order_id"], "state": "filled",
            "accFillSz": str(contracts), "avgPx": "2480", "instId": "ETH-USDT-SWAP",
        }]})

        state = (await database.fetch_all("SELECT state FROM trade_instances"))[0]["state"]
        protections = await database.fetch_all(
            "SELECT order_type,price,status,client_order_id FROM orders "
            "WHERE order_type IN ('TAKE_PROFIT','STOP_LOSS') ORDER BY id")
        trade_id = (await database.fetch_all("SELECT trade_id FROM trade_instances"))[0]["trade_id"]

        # 改止损
        stop = {"command_type": "MOVE_STOP", "exchange": "OKX", "base_asset": "ETH", "side": "LONG",
                "entry": None, "take_profits": [], "stop_loss": "2470", "quantity": None,
                "confidence": "1", "ambiguities": [], "trade_id": trade_id}
        move_report = await service.execute(command_from_json(stop, "tg-okx-move", settings))
        # 改止盈
        tp = dict(stop, command_type="AMEND_TAKE_PROFIT", stop_loss=None, take_profits=["2560"])
        tp_report = await service.execute(command_from_json(tp, "tg-okx-tp", settings))
        # 取消止损（应保留止盈）
        cancel = dict(stop, command_type="CANCEL_ORDER", stop_loss=None)
        cancel_report = await service.execute(command_from_json(cancel, "tg-okx-cancel", settings))

        final_state = (await database.fetch_all("SELECT state FROM trade_instances"))[0]["state"]
        live = await database.fetch_all(
            "SELECT order_type,status FROM orders WHERE status='NEW' ORDER BY order_type")
        # 对账一致性：本地开放订单编号必须与远端开放挂单完全一致。
        # 这是把 orders-algo-pending 合并进 get_open_orders 后的关键回归点。
        remote_ids = {order.client_order_id for order in await adapter.get_open_orders()}
        local_ids = {row["client_order_id"] for row in await database.fetch_all(
            "SELECT client_order_id FROM orders WHERE UPPER(status) IN "
            "('NEW','OPEN','PARTIALLY_FILLED','SUBMITTING') AND trade_id IN "
            "(SELECT trade_id FROM trade_instances WHERE exchange='OKX')")}
        await router.close()
        return (state, protections, move_report, tp_report, cancel_report, final_state,
                live, local_ids, remote_ids)

    (state, protections, move_report, tp_report, cancel_report, final_state,
     live, local_ids, remote_ids) = asyncio.run(scenario())
    # 成交后交易应进入 OPEN，并按真实持仓登记两张独立保护单。
    assert state == "OPEN"
    assert {row["order_type"] for row in protections} == {"TAKE_PROFIT", "STOP_LOSS"}
    # 三条人工指令都必须真正执行成功，而不是报「未找到活动本地订单」。
    assert "止损已更新为 2470" in move_report
    assert "止盈已更新为 2560" in tp_report
    assert "止损已取消" in cancel_report
    # 取消止损后进入 WAITING_ADD，且止盈必须仍然有效（OKX 旧 OCO 语义会连带撤掉止盈）。
    assert final_state == "WAITING_ADD"
    assert [row["order_type"] for row in live] == ["TAKE_PROFIT"]
    # 本地与远端开放订单编号必须一致，否则启动对账会误报状态不一致并禁用自动策略。
    assert local_ids == remote_ids
    assert len(remote_ids) == 1
