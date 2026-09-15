import asyncio
from decimal import Decimal

import pytest

from breakeven_strategy import calculate_breakeven, should_trigger, stop_only_improves
from codex_parser import _extract_json, build_subprocess_command, command_from_json
from database import Database
from exchange_router import ExchangeRouter
from exchanges.base import PaperAdapter
from models import Exchange, PositionSide, PositionSnapshot
from monitor import Monitor
from position_sizer import PositionSizer
from risk_manager import RiskManager
from settings import Settings
from trading_service import TradingService
from validator import CommandValidator, ValidationError


@pytest.fixture
def settings(tmp_path):
    config = tmp_path / "config.yaml"
    source = Settings.load("config.yaml").raw
    for exchange_config in source["exchanges"].values():
        exchange_config["mode"] = "LOCAL"
    import yaml
    config.write_text(yaml.safe_dump(source, allow_unicode=True), encoding="utf-8")
    return Settings.load(config)


def valid_payload():
    return {
        "command_type": "OPEN_POSITION", "exchange": "BINANCE", "base_asset": "ETH",
        "side": "LONG", "entry": {"type": "RANGE", "low": "2480", "high": "2490"},
        "take_profits": ["2519", "2549"], "stop_loss": "2455", "quantity": "0.01",
        "confidence": "0.98", "ambiguities": [],
    }


def test_parse_and_validate(settings):
    command = command_from_json(valid_payload(), "tg-1-1", settings)
    CommandValidator(settings).validate(command)
    assert command.entry.reference_price == Decimal("2485")


def test_invalid_long_stop_is_rejected(settings):
    payload = valid_payload()
    payload["stop_loss"] = "2490"
    command = command_from_json(payload, "tg-1-2", settings)
    with pytest.raises(ValidationError, match="止损"):
        CommandValidator(settings).validate(command)


def test_default_exchange_is_marked(settings):
    payload = valid_payload()
    payload["exchange"] = None
    command = command_from_json(payload, "tg-1-3", settings)
    assert command.exchange_defaulted is True
    assert command.exchange == settings.default_exchange


def test_chinese_asset_alias_gold_normalized_and_validated(settings):
    """测试中文标的名称（如‘黄金’）自动转换为 XAU 并通过白名单校验。"""
    payload = valid_payload()
    payload["base_asset"] = "黄金"
    command = command_from_json(payload, "tg-gold-1", settings)
    assert command.base_asset == "XAU"
    CommandValidator(settings).validate(command)


def test_breakeven_long_and_short():
    trigger, stop = calculate_breakeven(PositionSide.LONG, Decimal("100"), Decimal("120"),
                                         Decimal("0.5"), Decimal("0.01"))
    assert (trigger, stop) == (Decimal("110.0"), Decimal("101.00"))
    assert should_trigger(PositionSide.LONG, Decimal("110"), trigger)
    assert stop_only_improves(PositionSide.LONG, Decimal("95"), stop)
    short_trigger, short_stop = calculate_breakeven(PositionSide.SHORT, Decimal("100"), Decimal("80"),
                                                     Decimal("0.5"), Decimal("0.01"))
    assert (short_trigger, short_stop) == (Decimal("90.0"), Decimal("99.00"))


def test_position_uses_two_percent_margin_at_100x(settings):
    command = command_from_json(valid_payload(), "tg-size-1", settings)
    sizer = PositionSizer(settings)
    equity = Decimal("10000")
    quantity = sizer.calculate(command, equity)
    assert quantity * command.entry.reference_price == equity * Decimal("0.02") * Decimal("100")


def test_no_additional_stop_loss_risk_limit(settings):
    payload = valid_payload()
    payload["stop_loss"] = "1"
    command = command_from_json(payload, "tg-risk-1", settings)
    sizer = PositionSizer(settings)
    equity = Decimal("10000")
    from dataclasses import replace
    sized = replace(command, quantity=sizer.calculate(command, equity))
    # 客户没有要求按止损距离限制风险，因此这里只校验仓位名义价值。
    RiskManager(settings).check_open(sized, equity)


def test_windows_cmd_codex_command(monkeypatch):
    monkeypatch.setattr("codex_parser.os.name", "nt")
    executable, arguments = build_subprocess_command(
        r"C:\Tools\codex.CMD", ["exec", "--skip-git-repo-check", "-"]
    )
    assert executable.lower().endswith("cmd.exe")
    assert arguments[:3] == ["/d", "/s", "/c"]
    assert "codex.CMD" in arguments[3]


def test_extract_json_does_not_select_nested_breakeven():
    output = '{"command_type":"OPEN_POSITION","breakeven":{"trigger_ratio":"0.5"}}'
    assert _extract_json(output)["command_type"] == "OPEN_POSITION"


def test_paper_order_is_idempotent(settings):
    async def scenario():
        database = Database(settings.database_path)
        database.initialize()
        router = ExchangeRouter(settings)
        service = TradingService(settings, database, router)
        command = command_from_json(valid_payload(), "tg-2-1", settings)
        report = await service.execute(command)
        duplicate = await service.execute(command)
        orders = await database.fetch_all("SELECT * FROM orders")
        adapter = router.get(command.exchange)
        open_orders = await adapter.get_open_orders()
        await router.close()
        return report, duplicate, orders, open_orders

    report, duplicate, orders, open_orders = asyncio.run(scenario())
    assert "已提交" in report
    assert "重复指令" in duplicate
    assert len(orders) == 1
    assert len(open_orders) == 1
    # 验证 PaperAdapter 订单中包含了附带的第一止盈目标与止损价格
    assert open_orders[0].raw.get("take_profit_price") == "2519"
    assert open_orders[0].raw.get("stop_loss_price") == "2455"


def test_filled_entry_creates_protection_orders_and_opens_trade(settings):
    """成交事件应推进状态，并为非原子保护单交易所补建止盈止损。"""
    class DeferredProtectionPaperAdapter(PaperAdapter):
        @property
        def entry_protection_attached(self) -> bool:
            return False

    async def scenario():
        database = Database(settings.database_path)
        database.initialize()
        router = ExchangeRouter(settings)
        original = router.get(Exchange.BINANCE)
        adapter = DeferredProtectionPaperAdapter(original.instruments, original.equity)
        router.adapters[Exchange.BINANCE] = adapter
        service = TradingService(settings, database, router)
        command = command_from_json(valid_payload(), "tg-fill-1", settings)
        await service.execute(command)
        entry = (await database.fetch_all("SELECT * FROM orders WHERE order_type='ENTRY'"))[0]
        adapter.positions = [PositionSnapshot(
            Exchange.BINANCE, command.instrument_key, command.side, Decimal("0.8"), Decimal("2480"),
        )]
        monitor = Monitor(router, database)
        await monitor.process_event(Exchange.BINANCE, adapter, {
            "data": {"e": "ORDER_TRADE_UPDATE", "o": {"c": entry["client_order_id"], "X": "FILLED"}},
        })
        orders = await database.fetch_all("SELECT order_type,status FROM orders ORDER BY id")
        trade = (await database.fetch_all("SELECT state FROM trade_instances"))[0]
        await router.close()
        return orders, trade

    orders, trade = asyncio.run(scenario())
    assert [row["order_type"] for row in orders] == ["ENTRY", "TAKE_PROFIT", "STOP_LOSS"]
    assert trade["state"] == "OPEN"


def test_market_event_moves_stop_to_breakeven_once(settings):
    """达到目标 50% 后应以真实均价上移止损，且不重复触发。"""
    class DeferredProtectionPaperAdapter(PaperAdapter):
        @property
        def entry_protection_attached(self) -> bool:
            return False

    async def scenario():
        database = Database(settings.database_path)
        database.initialize()
        router = ExchangeRouter(settings)
        original = router.get(Exchange.BINANCE)
        adapter = DeferredProtectionPaperAdapter(original.instruments, original.equity)
        router.adapters[Exchange.BINANCE] = adapter
        command = command_from_json(valid_payload(), "tg-breakeven-1", settings)
        await TradingService(settings, database, router).execute(command)
        entry = (await database.fetch_all("SELECT * FROM orders WHERE order_type='ENTRY'"))[0]
        adapter.positions = [PositionSnapshot(
            Exchange.BINANCE, command.instrument_key, command.side, Decimal("0.8"), Decimal("2480"),
        )]
        monitor = Monitor(router, database)
        await monitor.process_event(Exchange.BINANCE, adapter, {
            "data": {"e": "ORDER_TRADE_UPDATE", "o": {"c": entry["client_order_id"], "X": "FILLED"}},
        })
        # 最终止盈 2549 的 50% 触发价为 2514.5，2520 已满足触发条件。
        tick = {"data": {"e": "markPriceUpdate", "s": "ETHUSDT", "p": "2520"}}
        await monitor.process_event(Exchange.BINANCE, adapter, tick)
        await monitor.process_event(Exchange.BINANCE, adapter, tick)
        stops = await database.fetch_all(
            "SELECT price,status FROM orders WHERE order_type='STOP_LOSS' ORDER BY id"
        )
        trade = (await database.fetch_all("SELECT breakeven_triggered FROM trade_instances"))[0]
        await router.close()
        return stops, trade

    stops, trade = asyncio.run(scenario())
    assert [(row["price"], row["status"]) for row in stops] == [("2455", "CANCELED"), ("2504.80", "NEW")]
    assert trade["breakeven_triggered"] == 1
