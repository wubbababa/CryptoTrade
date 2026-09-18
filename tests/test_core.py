import asyncio
from dataclasses import replace
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
from risk_manager import RiskError, RiskManager
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


def test_legacy_default_exchange_config_is_compatible(settings):
    """旧版单值配置应继续作为单元素默认目标列表工作。"""
    source = dict(settings.raw)
    source["trading"] = dict(source["trading"])
    source["trading"].pop("default_exchanges", None)
    source["trading"]["default_exchange"] = "OKX"
    legacy = Settings(source, settings.root)
    assert legacy.default_exchanges == (Exchange.OKX,)


def test_unspecified_exchange_broadcasts_to_all_available_defaults(settings):
    """未指定交易所的开仓应为每个默认本地适配器分别创建订单。"""
    async def scenario():
        database = Database(settings.database_path)
        database.initialize()
        router = ExchangeRouter(settings)
        service = TradingService(settings, database, router)
        payload = valid_payload()
        payload["exchange"] = None
        report = await service.execute(command_from_json(payload, "tg-broadcast-1", settings))
        trades = await database.fetch_all("SELECT exchange FROM trade_instances ORDER BY exchange")
        await router.close()
        return report, trades

    report, trades = asyncio.run(scenario())
    assert "公告未指定交易所" in report
    assert [row["exchange"] for row in trades] == ["BINANCE", "GATE", "OKX"]


def test_broadcast_skips_unavailable_exchange_and_continues(settings):
    """单家适配器不可用时，其余默认交易所仍须完成独立执行。"""
    async def scenario():
        database = Database(settings.database_path)
        database.initialize()
        router = ExchangeRouter(settings)
        router.adapters.pop(Exchange.GATE)
        router.unavailable_reasons[Exchange.GATE] = "测试连接失败"
        service = TradingService(settings, database, router)
        payload = valid_payload()
        payload["exchange"] = None
        report = await service.execute(command_from_json(payload, "tg-broadcast-2", settings))
        trades = await database.fetch_all("SELECT exchange FROM trade_instances ORDER BY exchange")
        await router.close()
        return report, trades

    report, trades = asyncio.run(scenario())
    assert "GATE（跳过：测试连接失败）" in report
    assert [row["exchange"] for row in trades] == ["BINANCE", "OKX"]


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
    # PaperAdapter 采用原子附带保护，进场单是唯一落库订单。
    assert [order["order_type"] for order in orders] == ["ENTRY"]
    assert len(open_orders) == 1
    # 验证 PaperAdapter 订单中包含了附带的第一止盈目标与止损价格
    assert open_orders[0].raw.get("take_profit_price") == "2519"
    assert open_orders[0].raw.get("stop_loss_price") == "2455"


def test_entry_acceptance_does_not_preplace_protection(settings):
    """进场受理后不得预挂保护单；保护单只能由成交事件按真实持仓补建。

    回归防护：Binance 在未持仓时预挂条件单会被 -4509
    （Time in Force GTE can only be used with open positions）拒绝。
    """
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
        command = command_from_json(valid_payload(), "tg-no-preplace", settings)
        report = await TradingService(settings, database, router).execute(command)
        orders = await database.fetch_all("SELECT order_type,status FROM orders ORDER BY id")
        trade = (await database.fetch_all("SELECT state FROM trade_instances"))[0]
        audits = await database.fetch_all(
            "SELECT category FROM audit_logs WHERE category='PLACE_PENDING_PROTECTION'"
        )
        await router.close()
        return report, orders, trade, audits

    report, orders, trade, audits = asyncio.run(scenario())
    assert "已提交" in report
    # 回报必须展示原始指令中的 TP/SL，并说明非原子交易所将在成交后补建。
    assert "止盈 2519/2549" in report
    assert "止损 2455" in report
    assert "成交后自动补建" in report
    # 只有进场单落库，没有任何预挂保护单，也没有预挂保护审计。
    assert [(row["order_type"], row["status"]) for row in orders] == [("ENTRY", "NEW")]
    assert audits == []
    assert trade["state"] == "PENDING_ENTRY"


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
            Exchange.BINANCE, command.instrument_key, command.side, Decimal(entry["quantity"]), Decimal("2480"),
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


def test_partial_fill_creates_and_resizes_protection_from_actual_quantity(settings):
    """部分成交应按累计实际成交量建保护，完全成交后替换为完整数量。"""
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
        command = command_from_json(valid_payload(), "tg-partial-1", settings)
        await TradingService(settings, database, router).execute(command)
        entry = (await database.fetch_all("SELECT client_order_id,quantity FROM orders WHERE order_type='ENTRY'"))[0]
        monitor = Monitor(router, database)
        adapter.positions = [PositionSnapshot(Exchange.BINANCE, command.instrument_key, command.side,
                                              Decimal("2"), Decimal("2480"))]
        await monitor.process_event(Exchange.BINANCE, adapter, {
            "data": {"e": "ORDER_TRADE_UPDATE", "o": {"c": entry["client_order_id"], "X": "PARTIALLY_FILLED",
                                                              "z": "2", "ap": "2480"}},
        })
        adapter.positions = [PositionSnapshot(Exchange.BINANCE, command.instrument_key, command.side,
                                              Decimal(entry["quantity"]), Decimal("2480"))]
        await monitor.process_event(Exchange.BINANCE, adapter, {
            "data": {"e": "ORDER_TRADE_UPDATE", "o": {"c": entry["client_order_id"], "X": "FILLED",
                                                              "z": entry["quantity"], "ap": "2480"}},
        })
        entry_row = (await database.fetch_all("SELECT filled_quantity FROM orders WHERE order_type='ENTRY'"))[0]
        protections = await database.fetch_all("SELECT quantity,status FROM orders WHERE order_type='STOP_LOSS'")
        trade = (await database.fetch_all("SELECT state FROM trade_instances"))[0]
        await router.close()
        return entry, entry_row, protections, trade

    entry, entry_row, protections, trade = asyncio.run(scenario())
    assert entry_row["filled_quantity"] == entry["quantity"]
    # 新流程不再在进场受理阶段预挂保护单，因此只剩「部分成交量」与「完整量」两条记录。
    assert [(row["quantity"], row["status"]) for row in protections] == [
        ("2", "CANCELED"),                 # 部分成交后按实际数量创建的保护单
        (entry["quantity"], "NEW"),         # 全部成交后按完整数量校准的保护单
    ]
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
            Exchange.BINANCE, command.instrument_key, command.side, Decimal(entry["quantity"]), Decimal("2480"),
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


def test_reconcile_recovers_unique_position_and_creates_missing_protection(settings):
    """重启后唯一可关联的实际持仓应恢复保本监控，并补建缺失保护单。"""
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
        command = command_from_json(valid_payload(), "tg-restart-1", settings)
        await TradingService(settings, database, router).execute(command)
        entry = (await database.fetch_all("SELECT exchange_order_id,quantity FROM orders WHERE order_type='ENTRY'"))[0]
        adapter.orders[entry["exchange_order_id"]] = replace(
            adapter.orders[entry["exchange_order_id"]], status="FILLED",
        )
        adapter.positions = [PositionSnapshot(
            Exchange.BINANCE, command.instrument_key, command.side, Decimal(entry["quantity"]), Decimal("2480"),
        )]
        warnings = await Monitor(router, database).reconcile()
        trade = (await database.fetch_all("SELECT state FROM trade_instances"))[0]
        orders = await database.fetch_all("SELECT order_type FROM orders ORDER BY id")
        await router.close()
        return warnings, trade, orders

    warnings, trade, orders = asyncio.run(scenario())
    assert warnings == []
    assert trade["state"] == "OPEN"
    assert [row["order_type"] for row in orders] == ["ENTRY", "TAKE_PROFIT", "STOP_LOSS"]


def test_reconcile_does_not_guess_untracked_position(settings):
    """交易所中没有唯一关联记录的持仓必须仅告警，不能创建或修改订单。"""
    async def scenario():
        database = Database(settings.database_path)
        database.initialize()
        router = ExchangeRouter(settings)
        adapter = router.get(Exchange.BINANCE)
        adapter.positions = [PositionSnapshot(
            Exchange.BINANCE, "ETH/USDT:PERP", PositionSide.LONG, Decimal("0.8"), Decimal("2480"),
        )]
        warnings = await Monitor(router, database).reconcile()
        orders = await database.fetch_all("SELECT * FROM orders")
        await router.close()
        return warnings, orders

    warnings, orders = asyncio.run(scenario())
    assert len(warnings) == 1
    assert "未关联远程持仓" in warnings[0]
    assert orders == []


def test_multiple_same_asset_trades_use_exact_quantity_allocation(settings):
    """同币种多笔交易只有数量精确匹配汇总仓位时，才可逐笔移动止损。"""
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
        first = command_from_json(valid_payload(), "tg-multi-1", settings)
        second = command_from_json(valid_payload(), "tg-multi-2", settings)
        await service.execute(first)
        await service.execute(second)
        entries = await database.fetch_all("SELECT client_order_id,quantity FROM orders WHERE order_type='ENTRY' ORDER BY id")
        total = sum((Decimal(row["quantity"]) for row in entries), Decimal("0"))
        adapter.positions = [PositionSnapshot(
            Exchange.BINANCE, first.instrument_key, first.side, total, Decimal("2480"),
        )]
        monitor = Monitor(router, database)
        for entry in entries:
            await monitor.process_event(Exchange.BINANCE, adapter, {
                "data": {"e": "ORDER_TRADE_UPDATE", "o": {"c": entry["client_order_id"], "X": "FILLED"}},
            })
        await monitor.process_event(Exchange.BINANCE, adapter, {
            "data": {"e": "markPriceUpdate", "s": "ETHUSDT", "p": "2520"},
        })
        stops = await database.fetch_all("SELECT quantity,status FROM orders WHERE order_type='STOP_LOSS'")
        await router.close()
        return entries, stops

    entries, stops = asyncio.run(scenario())
    assert len(entries) == 2
    assert len(stops) == 4
    assert [row["status"] for row in stops].count("NEW") == 2
    assert {row["quantity"] for row in stops if row["status"] == "NEW"} == {row["quantity"] for row in entries}


def test_multiple_same_asset_trades_stop_when_remote_quantity_is_not_exact(settings):
    """外部加仓或漏记成交造成数量不一致时，自动保本必须停止。"""
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
        command = command_from_json(valid_payload(), "tg-association-1", settings)
        await TradingService(settings, database, router).execute(command)
        entry = (await database.fetch_all("SELECT client_order_id,quantity FROM orders WHERE order_type='ENTRY'"))[0]
        adapter.positions = [PositionSnapshot(
            Exchange.BINANCE, command.instrument_key, command.side,
            Decimal(entry["quantity"]) + Decimal("1"), Decimal("2480"),
        )]
        monitor = Monitor(router, database)
        await monitor.process_event(Exchange.BINANCE, adapter, {
            "data": {"e": "ORDER_TRADE_UPDATE", "o": {"c": entry["client_order_id"], "X": "FILLED"}},
        })
        await monitor.process_event(Exchange.BINANCE, adapter, {
            "data": {"e": "markPriceUpdate", "s": "ETHUSDT", "p": "2520"},
        })
        stops = await database.fetch_all("SELECT status FROM orders WHERE order_type='STOP_LOSS'")
        audits = await database.fetch_all("SELECT result FROM audit_logs WHERE category='POSITION_ASSOCIATION'")
        await router.close()
        return stops, audits

    stops, audits = asyncio.run(scenario())
    assert [row["status"] for row in stops] == ["NEW"]
    assert audits and audits[0]["result"] == "MISMATCH"


def test_amend_entry_requires_exact_remote_order_and_updates_price(settings):
    """指定 trade_id 的改挂单只能修改交易所中仍存在的同一客户订单。"""
    async def scenario():
        database = Database(settings.database_path)
        database.initialize()
        router = ExchangeRouter(settings)
        service = TradingService(settings, database, router)
        await service.execute(command_from_json(valid_payload(), "tg-amend-open", settings))
        trade_id = (await database.fetch_all("SELECT trade_id FROM trade_instances"))[0]["trade_id"]
        payload = valid_payload()
        payload.update({"command_type": "AMEND_ENTRY", "trade_id": trade_id,
                        "entry": {"type": "LIMIT", "low": "2470", "high": "2470"},
                        "take_profits": [], "stop_loss": None})
        report = await service.execute(command_from_json(payload, "tg-amend-change", settings))
        order = (await database.fetch_all("SELECT price FROM orders WHERE order_type='ENTRY'"))[0]
        await router.close()
        return report, order

    report, order = asyncio.run(scenario())
    assert "2470" in report
    assert order["price"] == "2470"


def test_cancel_stop_then_restore_stop_uses_trade_id(settings):
    """已开仓后取消止损会进入 WAITING_ADD，恢复止损必须继续使用同一交易编号。"""
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
        opening = command_from_json(valid_payload(), "tg-manual-open", settings)
        await service.execute(opening)
        entry = (await database.fetch_all("SELECT client_order_id,quantity FROM orders WHERE order_type='ENTRY'"))[0]
        adapter.positions = [PositionSnapshot(Exchange.BINANCE, opening.instrument_key, opening.side,
                                              Decimal(entry["quantity"]), Decimal("2480"))]
        monitor = Monitor(router, database)
        await monitor.process_event(Exchange.BINANCE, adapter, {
            "data": {"e": "ORDER_TRADE_UPDATE", "o": {"c": entry["client_order_id"], "X": "FILLED"}},
        })
        trade_id = (await database.fetch_all("SELECT trade_id FROM trade_instances"))[0]["trade_id"]
        cancel = valid_payload()
        cancel.update({"command_type": "CANCEL_ORDER", "trade_id": trade_id,
                       "entry": None, "take_profits": [], "stop_loss": None})
        await service.execute(command_from_json(cancel, "tg-cancel-stop", settings))
        restore = valid_payload()
        restore.update({"command_type": "MOVE_STOP", "trade_id": trade_id,
                        "entry": None, "take_profits": [], "stop_loss": "2470"})
        report = await service.execute(command_from_json(restore, "tg-restore-stop", settings))
        trade = (await database.fetch_all("SELECT state FROM trade_instances"))[0]
        stops = await database.fetch_all("SELECT price,status FROM orders WHERE order_type='STOP_LOSS' ORDER BY id")
        await router.close()
        return report, trade, stops

    report, trade, stops = asyncio.run(scenario())
    assert "2470" in report
    assert trade["state"] == "OPEN"
    assert [(row["price"], row["status"]) for row in stops] == [("2455", "CANCELED"), ("2470", "NEW")]


def test_notional_exactly_at_exchange_limit_is_not_rejected(settings):
    """仓位数量带除法舍入误差时，恰好用满交易所额度的指令不得被误拒。

    线上表现：Gate 权益 1999、进场价 2477 时名义价值算成 3998.000000000000000000000001，
    超过上限 3998 而被拒绝，日志为「失败：超过该交易所最大持仓名义价值」。
    """
    payload = valid_payload()
    payload.update({"exchange": "GATE", "entry": {"type": "RANGE", "low": "2475", "high": "2479"}})
    command = command_from_json(payload, "tg-limit-1", settings)
    equity = Decimal("1999")
    assert command.entry is not None
    # 仓位数量 = 权益 × 保证金比例 × 杠杆 ÷ 价格，对应名义价值恰好等于「2 倍权益」的上限。
    quantity = equity * Decimal("0.02") * Decimal("100") / command.entry.reference_price
    RiskManager(settings).check_open(replace(command, quantity=quantity), equity)
    with pytest.raises(RiskError, match="最大持仓名义价值"):
        RiskManager(settings).check_open(replace(command, quantity=Decimal("1.62")), equity)


def test_entry_quantity_is_recorded_as_step_rounded_submitted_quantity(settings):
    """本地必须记录交易所实际收到的取整数量，否则重启恢复时无法与远程持仓对齐。"""
    async def scenario():
        database = Database(settings.database_path)
        database.initialize()
        router = ExchangeRouter(settings)
        adapter = router.get(Exchange.BINANCE)
        adapter.equity = Decimal("1999")
        payload = valid_payload()
        payload["entry"] = {"type": "RANGE", "low": "2475", "high": "2479"}
        await TradingService(settings, database, router).execute(
            command_from_json(payload, "tg-step-1", settings),
        )
        row = (await database.fetch_all(
            "SELECT quantity,exchange_order_id FROM orders WHERE order_type='ENTRY'"))[0]
        submitted = adapter.orders[row["exchange_order_id"]].raw["quantity"]
        await router.close()
        return row["quantity"], submitted

    stored, submitted = asyncio.run(scenario())
    # 1999 × 2 ÷ 2477 = 1.6140492…，按 0.001 步进向下取整后才是提交给交易所的数量。
    assert stored == submitted == "1.614"


def test_okx_contract_fills_use_base_quantity_for_breakeven(settings):
    """OKX 以合约张数回报成交：换算后本地数量与远程仓位一致，汇总仓位才允许自动保本。"""
    class ContractSizePaperAdapter(PaperAdapter):
        """模拟 OKX ETH-USDT-SWAP：1 张合约 = 0.1 ETH，且保护单需成交后补建。"""

        @property
        def entry_protection_attached(self) -> bool:
            return False

    async def scenario():
        database = Database(settings.database_path)
        database.initialize()
        router = ExchangeRouter(settings)
        original = router.get(Exchange.OKX)
        instruments = {
            asset: replace(instrument, quantity_step=Decimal("0.1"), minimum_quantity=Decimal("0.1"),
                           contract_multiplier=Decimal("0.1"))
            for asset, instrument in original.instruments.items()
        }
        adapter = ContractSizePaperAdapter(instruments, Decimal("1999"))
        router.adapters[Exchange.OKX] = adapter
        payload = valid_payload()
        payload.update({"exchange": "OKX", "entry": {"type": "RANGE", "low": "2475", "high": "2479"}})
        command = command_from_json(payload, "tg-okx-units-1", settings)
        await TradingService(settings, database, router).execute(command)
        entry = (await database.fetch_all(
            "SELECT client_order_id,quantity FROM orders WHERE order_type='ENTRY'"))[0]
        quantity = Decimal(entry["quantity"])
        adapter.positions = [PositionSnapshot(
            Exchange.OKX, command.instrument_key, command.side, quantity, Decimal("2477"),
        )]
        monitor = Monitor(router, database)
        await monitor.process_event(Exchange.OKX, adapter, {
            "arg": {"channel": "orders"}, "data": [{
                "clOrdId": entry["client_order_id"], "state": "filled",
                "accFillSz": str(quantity / Decimal("0.1")), "avgPx": "2477",
                "instId": "ETH-USDT-SWAP",
            }],
        })
        filled = (await database.fetch_all(
            "SELECT filled_quantity FROM orders WHERE order_type='ENTRY'"))[0]["filled_quantity"]
        # 最终止盈 2549、均价 2477：50% 触发价 2513，标记价 2520 已满足保本条件。
        await monitor.process_event(Exchange.OKX, adapter, {
            "arg": {"channel": "tickers"}, "data": [{"instId": "ETH-USDT-SWAP", "last": "2520"}],
        })
        stops = await database.fetch_all(
            "SELECT price,status FROM orders WHERE order_type='STOP_LOSS' ORDER BY id")
        trade = (await database.fetch_all("SELECT state,breakeven_triggered FROM trade_instances"))[0]
        await router.close()
        return quantity, filled, stops, trade

    quantity, filled, stops, trade = asyncio.run(scenario())
    assert quantity == Decimal("1.6")
    assert Decimal(filled) == quantity
    assert trade["state"] == "OPEN"
    assert trade["breakeven_triggered"] == 1
    assert [(row["price"], row["status"]) for row in stops] == [("2455", "CANCELED"), ("2501.77", "NEW")]
