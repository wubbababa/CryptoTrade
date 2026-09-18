"""Telegram 对话命令层测试：人工指令解析、安全边界与消息分发。"""

import asyncio

import pytest
import yaml

from database import Database
from exchange_router import ExchangeRouter
from exchanges.base import PaperAdapter
from main import Application
from models import Exchange, PositionSide, PositionSnapshot
from settings import Settings
from telegram_client import TelegramCallback, TelegramMessage, TelegramClient, _optional_chat_id_list
from telegram_commands import (
    CommandRejected,
    CommandReply,
    TelegramCommandHandler,
    UnknownCommand,
    _truncate,
    parse_command,
    parse_price,
)
from telegram_menu import (
    STATE_ACTIONS,
    decode_callback,
    encode_callback,
    fingerprint,
    main_menu_keyboard,
    trades_keyboard,
)
from decimal import Decimal

from trading_service import TradingService


def build_settings(tmp_path, mode="LOCAL"):
    config_path = tmp_path / "config.yaml"
    source = Settings.load("config.yaml").raw
    for exchange_config in source["exchanges"].values():
        exchange_config["mode"] = mode
    config_path.write_text(yaml.safe_dump(source, allow_unicode=True), encoding="utf-8")
    return Settings.load(config_path)


class FakeTelegram:
    """记录 reply/report/edit_reply/answer_callback 调用，不访问网络。"""

    def __init__(self) -> None:
        self.replies: list[tuple[int, str]] = []
        self.keyboards: list[list[list[dict]] | None] = []
        self.edits: list[tuple[int, int, str, list[list[dict]] | None]] = []
        self.answers: list[str] = []
        self.reports: list[str] = []
        self.bot_username = "Crypto20260909_bot"

    async def reply(self, chat_id: int, text: str, keyboard=None) -> None:
        self.replies.append((chat_id, text))
        self.keyboards.append(keyboard)

    async def edit_reply(self, chat_id: int, message_id: int, text: str, keyboard=None) -> None:
        self.edits.append((chat_id, message_id, text, keyboard))

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.answers.append(callback_id)

    async def report(self, text: str) -> None:
        self.reports.append(text)


def _callback_button_texts(keyboard) -> list[str]:
    """摊平内联键盘，取出全部按钮文案，便于断言。"""
    return [button["text"] for row in keyboard or [] for button in row]


def _callback_data(keyboard, text: str) -> str:
    """按按钮文案取出其 callback_data。"""
    for row in keyboard or []:
        for button in row:
            if button["text"] == text:
                return button["callback_data"]
    raise AssertionError(f"未找到按钮：{text}")


class DeferredProtectionPaperAdapter(PaperAdapter):
    """模拟 OKX/Binance/Gate：保护单需在成交后补建（非原子附带）。"""

    @property
    def entry_protection_attached(self) -> bool:
        return False


class ExplodingAdapter(PaperAdapter):
    """任何写操作都应导致测试失败，用于证明命令层不触碰交易所。"""

    def __init__(self, instruments, equity):
        super().__init__(instruments, equity)
        self.calls: list[str] = []

    def _record(self, name: str):
        self.calls.append(name)
        raise AssertionError(f"命令层不应调用交易所写接口：{name}")

    async def amend_entry_order(self, order_id, request):
        self._record("amend_entry_order")

    async def cancel_order(self, order_id):
        self._record("cancel_order")

    async def place_stop_loss(self, request):
        self._record("place_stop_loss")

    async def close_position(self, request):
        self._record("close_position")


@pytest.fixture
def handler(tmp_path):
    settings = build_settings(tmp_path)
    database = Database(tmp_path / "trading.db")
    database.initialize()
    router = ExchangeRouter(settings)
    service = TradingService(settings, database, router)
    return TelegramCommandHandler(database, router, service), database, service, router


def _payload():
    return {
        "command_type": "OPEN_POSITION", "exchange": "BINANCE", "base_asset": "ETH",
        "side": "LONG", "entry": {"type": "RANGE", "low": "2480", "high": "2490"},
        "take_profits": ["2519", "2549"], "stop_loss": "2455", "quantity": "0.01",
        "confidence": "0.98", "ambiguities": [],
    }


def test_parse_command_plain_and_with_args():
    assert parse_command("/start") == ("start", ())
    assert parse_command("  /status  ") == ("status", ())
    assert parse_command("/help extra arg") == ("help", ("extra", "arg"))
    assert parse_command("/amend_entry TID 2470") == ("amend_entry", ("TID", "2470"))


def test_parse_command_is_case_insensitive():
    assert parse_command("/AMEND_ENTRY x 1") == ("amend_entry", ("x", "1"))


def test_parse_command_ignores_non_commands_and_other_bots():
    assert parse_command("ETH2480附近多") is None
    assert parse_command("") is None
    # 已知自身用户名时，@ 其他机器人必须忽略。
    assert parse_command("/status@other_bot", "Crypto20260909_bot") is None
    # 未知自身用户名时无法判断归属，宽松放行并剥掉后缀。
    assert parse_command("/status@other_bot") == ("status", ())


def test_parse_command_accepts_own_username():
    assert parse_command("/status@Crypto20260909_bot", "Crypto20260909_bot") == ("status", ())
    assert parse_command("/STATUS", "Crypto20260909_bot") == ("status", ())


def test_parse_price_rejects_invalid_values():
    assert parse_price("2470.5", "价格") == Decimal("2470.5")
    assert parse_price("2470", "价格") == Decimal("2470")
    for bad in ("0", "-1", "abc", "NaN", "Infinity"):
        with pytest.raises(CommandRejected):
            parse_price(bad, "价格")


def test_command_chat_id_list_parsing(monkeypatch):
    assert _optional_chat_id_list("-1001234567890, 5338691895") == [-1001234567890, 5338691895]
    # 拒绝把邀请链接当作命令来源。
    with pytest.raises(RuntimeError, match="邀请链接"):
        _optional_chat_id_list("https://t.me/+example")


def test_handler_replies_start_and_help(handler):
    command_handler, _, _, _ = handler

    async def scenario():
        return await command_handler.execute("start"), await command_handler.execute("help")

    start_text, help_text = asyncio.run(scenario())
    assert "/status" in start_text
    assert start_text == help_text
    # 帮助必须列出全部人工指令，避免入口不可发现。
    for name in ("/amend_entry", "/cancel_order", "/cancel_stop", "/move_stop", "/close_position"):
        assert name in start_text


def test_handler_rejects_unknown_command(handler):
    command_handler, _, _, _ = handler

    async def scenario():
        with pytest.raises(UnknownCommand):
            await command_handler.execute("rm-rf")

    asyncio.run(scenario())


def test_handler_status_reports_trades_and_equity(handler):
    command_handler, database, _, router = handler

    async def scenario():
        await database.execute(
            "INSERT INTO trade_instances(trade_id,exchange,instrument_key,side,state) "
            "VALUES('OKX-ETH-USDT-PERP-LONG-20260916-001','OKX','ETH/USDT:PERP','LONG','PENDING_ENTRY')"
        )
        await database.execute(
            "INSERT INTO trade_instances(trade_id,exchange,instrument_key,side,state) "
            "VALUES('OKX-ETH-USDT-PERP-LONG-20260916-002','OKX','ETH/USDT:PERP','LONG','CLOSED')"
        )
        await database.execute(
            "INSERT INTO trade_instances(trade_id,exchange,instrument_key,side,state) "
            "VALUES('OKX-BTC-USDT-PERP-SHORT-20260916-003','OKX','BTC/USDT:PERP','SHORT','REJECTED')"
        )
        return await command_handler.execute("status")

    try:
        text = asyncio.run(scenario())
    finally:
        asyncio.run(router.close())
    assert "PENDING_ENTRY" in text
    assert "OKX-ETH-USDT-PERP-LONG-20260916-001" in text
    assert "CLOSED" not in text.split("【活动交易】")[1].split("【")[0]
    assert "权益" in text


def test_handler_status_survives_broken_exchange(handler):
    command_handler, _, _, router = handler
    # 让适配器读取权益时抛错，状态查询仍应返回文本。
    for adapter in command_handler.router.adapters.values():
        async def boom():
            raise RuntimeError("network down")

        adapter.get_equity = boom

    try:
        text = asyncio.run(command_handler.execute("status"))
    finally:
        asyncio.run(router.close())
    assert "读取失败" in text


def test_trades_text_lists_active_trade_ids(handler):
    command_handler, database, _, router = handler

    async def scenario():
        await database.execute(
            "INSERT INTO trade_instances(trade_id,exchange,instrument_key,side,state) "
            "VALUES('BINANCE-ETH-USDT-PERP-LONG-20260916-007','BINANCE','ETH/USDT:PERP','LONG','OPEN')"
        )
        return await command_handler.execute("trades")

    try:
        text = asyncio.run(scenario())
    finally:
        asyncio.run(router.close())
    assert "BINANCE-ETH-USDT-PERP-LONG-20260916-007" in text
    assert "OPEN" in text


def test_truncate_keeps_short_text_and_marks_long_text():
    assert _truncate("短文本") == "短文本"
    truncated = _truncate("x" * 5000, limit=100)
    assert len(truncated) <= 100
    assert "截断" in truncated


# ----------------------------------------------------------------------
# 人工指令：参数校验与拒绝路径
# ----------------------------------------------------------------------

def test_manual_command_without_trade_id_is_rejected(handler):
    command_handler, _, _, router = handler

    async def scenario():
        with pytest.raises(CommandRejected, match="缺少参数"):
            await command_handler.execute("close_position", (), "tg-1-1")

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(router.close())


def test_manual_command_with_unknown_trade_id_is_rejected(handler):
    command_handler, _, _, router = handler

    async def scenario():
        with pytest.raises(CommandRejected, match="交易编号不存在"):
            await command_handler.execute("close_position", ("NOPE-1",), "tg-1-2")

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(router.close())


def test_manual_command_rejects_extra_arguments(handler):
    command_handler, database, _, router = handler

    async def scenario():
        await database.execute(
            "INSERT INTO trade_instances(trade_id,exchange,instrument_key,side,state) "
            "VALUES('BINANCE-ETH-USDT-PERP-LONG-20260916-008','BINANCE','ETH/USDT:PERP','LONG','OPEN')"
        )
        with pytest.raises(CommandRejected, match="不接受额外参数"):
            await command_handler.execute("close_position", ("BINANCE-ETH-USDT-PERP-LONG-20260916-008", "x"), "tg-1-3")

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(router.close())


def test_move_stop_usage_lists_both_variants(handler):
    """move_stop 的用法提示应同时给出「指定价格」与「保本恢复」两种写法。"""
    command_handler, _, _, router = handler
    try:
        usage = command_handler._manual_usage("move_stop")
    finally:
        asyncio.run(router.close())
    assert "/move_stop <trade_id> <价格>" in usage
    assert "/move_stop <trade_id> breakeven" in usage


def test_manual_command_requires_service(handler):
    command_handler, _, _, router = handler
    command_handler.service = None

    async def scenario():
        with pytest.raises(CommandRejected, match="未接入交易服务"):
            await command_handler.execute("close_position", ("X",), "tg-1-4")

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(router.close())


def test_amend_entry_builds_limit_entry_and_delegates(tmp_path):
    """人工 /amend_entry 应把单点价格包装成 LIMIT 进场并真正改价。"""
    workspace = tmp_path / "amend"
    workspace.mkdir(exist_ok=True)
    settings, database, router, adapter, service, command = _build_manual_scenario(workspace)
    command_handler = TelegramCommandHandler(database, router, service)

    async def scenario():
        await service.execute(command)
        trade_id = (await database.fetch_all("SELECT trade_id FROM trade_instances"))[0]["trade_id"]
        return await command_handler.execute("amend_entry", (trade_id, "2470"), "tg-9-1"), trade_id

    try:
        report, trade_id = asyncio.run(scenario())
        price = asyncio.run(database.fetch_all("SELECT price FROM orders WHERE order_type='ENTRY'"))[0]["price"]
    finally:
        asyncio.run(router.close())
    assert "2470" in report
    assert trade_id in report
    assert price == "2470"


def test_build_manual_command_fills_exchange_contract_and_side(handler):
    command_handler, database, _, router = handler

    async def scenario():
        await database.execute(
            "INSERT INTO trade_instances(trade_id,exchange,instrument_key,side,state) "
            "VALUES('GATE-BTC-USDT-PERP-SHORT-20260916-009','GATE','BTC/USDT:PERP','SHORT','OPEN')"
        )
        return await command_handler._build_manual_command(
            "move_stop", ("GATE-BTC-USDT-PERP-SHORT-20260916-009", "60000"), "tg-9-2",
        )

    try:
        command = asyncio.run(scenario())
    finally:
        asyncio.run(router.close())
    assert command.exchange is Exchange.GATE
    assert command.base_asset == "BTC"
    assert command.side is PositionSide.SHORT
    assert str(command.stop_loss) == "60000"
    assert command.trade_id == "GATE-BTC-USDT-PERP-SHORT-20260916-009"
    assert command.command_id == "tg-9-2-move_stop"


def test_move_stop_breakeven_keyword_leaves_stop_loss_empty(handler):
    command_handler, database, _, router = handler

    async def scenario():
        await database.execute(
            "INSERT INTO trade_instances(trade_id,exchange,instrument_key,side,state) "
            "VALUES('OKX-BTC-USDT-PERP-LONG-20260916-010','OKX','BTC/USDT:PERP','LONG','OPEN')"
        )
        return await command_handler._build_manual_command(
            "move_stop", ("OKX-BTC-USDT-PERP-LONG-20260916-010", "保本"), "tg-9-3",
        )

    try:
        command = asyncio.run(scenario())
    finally:
        asyncio.run(router.close())
    # 不带价格即恢复止损，由用例层按进场均价浮盈 1% 计算。
    assert command.stop_loss is None


def test_cancel_stop_alias_maps_to_cancel_order(handler):
    command_handler, database, _, router = handler

    async def scenario():
        await database.execute(
            "INSERT INTO trade_instances(trade_id,exchange,instrument_key,side,state) "
            "VALUES('OKX-BTC-USDT-PERP-LONG-20260916-011','OKX','BTC/USDT:PERP','LONG','OPEN')"
        )
        return await command_handler._build_manual_command(
            "cancel_stop", ("OKX-BTC-USDT-PERP-LONG-20260916-011",), "tg-9-4",
        )

    try:
        command = asyncio.run(scenario())
    finally:
        asyncio.run(router.close())
    assert command.command_type.value == "CANCEL_ORDER"


# ----------------------------------------------------------------------
# 端到端：人工指令经由 TradingService 生效，且命令层不直接触碰交易所
# ----------------------------------------------------------------------

def _build_manual_scenario(tmp_path):
    """搭建「已开仓 + 已有保护单」的交易环境，用于人工指令端到端验证。"""
    settings = build_settings(tmp_path)
    database = Database(tmp_path / "trading.db")
    database.initialize()
    router = ExchangeRouter(settings)
    original = router.get(Exchange.BINANCE)
    adapter = DeferredProtectionPaperAdapter(original.instruments, original.equity)
    router.adapters[Exchange.BINANCE] = adapter
    service = TradingService(settings, database, router)
    from codex_parser import command_from_json

    command = command_from_json(_payload(), "tg-manual-open", settings)
    return settings, database, router, adapter, service, command


async def _fill_entry(database, adapter, command, monitor, quantity="1.614", price="2480"):
    """把进场单标记为完全成交并补建保护单，使交易进入 OPEN。"""
    entry = (await database.fetch_all(
        "SELECT client_order_id FROM orders WHERE order_type='ENTRY'"))[0]
    adapter.positions = [PositionSnapshot(
        Exchange.BINANCE, command.instrument_key, command.side, Decimal(quantity), Decimal(price),
    )]
    await monitor.process_event(Exchange.BINANCE, adapter, {
        "data": {"e": "ORDER_TRADE_UPDATE",
                 "o": {"c": entry["client_order_id"], "X": "FILLED", "z": quantity, "ap": price}},
    })


def test_move_stop_end_to_end_creates_new_stop(tmp_path):
    """人工 /move_stop 应经用例层核对远程持仓后真的重建止损单。"""
    from monitor import Monitor

    workspace = tmp_path / "m"
    workspace.mkdir(exist_ok=True)
    settings, database, router, adapter, service, command = _build_manual_scenario(workspace)
    monitor = Monitor(router, database)

    async def scenario():
        await service.execute(command)
        await _fill_entry(database, adapter, command, monitor)
        trade_id = (await database.fetch_all("SELECT trade_id FROM trade_instances"))[0]["trade_id"]
        handler = TelegramCommandHandler(database, router, service)
        report = await handler.execute("move_stop", (trade_id, "2470"), "tg-10-1")
        stops = await database.fetch_all(
            "SELECT price,status FROM orders WHERE order_type='STOP_LOSS' ORDER BY id")
        trade = (await database.fetch_all("SELECT state FROM trade_instances"))[0]
        return report, trade_id, stops, trade

    try:
        report, trade_id, stops, trade = asyncio.run(scenario())
    finally:
        asyncio.run(router.close())
    assert "2470" in report
    # 旧止损被撤销、新止损按人工价格落地，交易仍保持 OPEN。
    assert [(row["price"], row["status"]) for row in stops[:2]] == [("2455", "CANCELED"), ("2470", "NEW")]
    assert trade["state"] == "OPEN"


def test_command_layer_never_calls_exchange_write_interfaces(handler):
    """命令层只解析与回复；交易所写接口必须由 TradingService 调用。"""
    _, database, _, router = handler
    original = router.get(Exchange.BINANCE)
    exploding = ExplodingAdapter(original.instruments, original.equity)
    router.adapters[Exchange.BINANCE] = exploding
    command_handler = TelegramCommandHandler(database, router, None)

    async def scenario():
        with pytest.raises(CommandRejected):
            await command_handler.execute("close_position", ("MISSING-1",), "tg-11-1")
        return exploding.calls

    try:
        calls = asyncio.run(scenario())
    finally:
        asyncio.run(router.close())
    assert calls == []



# ----------------------------------------------------------------------
# 内联按钮菜单：可点击执行
# ----------------------------------------------------------------------

def test_start_reply_carries_inline_keyboard(handler):
    """/start 必须返回可点击按钮，而不是只有纯文本。"""
    command_handler, _, _, router = handler

    async def scenario():
        return await command_handler.execute_reply("start", (), "tg-menu-1")

    try:
        reply = asyncio.run(scenario())
    finally:
        asyncio.run(router.close())
    assert isinstance(reply, CommandReply)
    assert reply.keyboard, "/start 必须带内联键盘"
    labels = _callback_button_texts(reply.keyboard)
    assert "📊 运行状态" in labels
    assert "📋 交易与操作" in labels
    assert "✏️ 人工指令" in labels


def test_all_menu_callbacks_round_trip_and_fit_limit():
    """所有菜单按钮的 callback_data 必须可解析且不超过 Telegram 64 字节限制。"""
    for keyboard in (main_menu_keyboard(), trades_keyboard(_sample_trades())):
        for row in keyboard:
            for button in row:
                data = button["callback_data"]
                assert len(data.encode("utf-8")) <= 64, data
                # 必须能被解析回动作，且写操作标记正确。
                action = decode_callback(data)
                assert action.kind in {"menu", "panel", "run", "pick", "go"}


def test_status_button_returns_refreshed_status(handler):
    """点击「运行状态」按钮应就地刷新状态并保留键盘。"""
    command_handler, _, _, router = handler
    data = _callback_data(main_menu_keyboard(), "📊 运行状态")

    async def scenario():
        return await command_handler.handle_callback(data)

    try:
        reply = asyncio.run(scenario())
    finally:
        asyncio.run(router.close())
    assert "CryptoTrade 状态" in reply.text
    assert reply.edit is True
    assert reply.keyboard


def test_trades_view_shows_per_trade_action_buttons(handler):
    """有活动交易时，按钮应逐笔列出且只展示该状态允许的动作。"""
    command_handler, database, _, router = handler

    async def scenario():
        await database.execute(
            "INSERT INTO trade_instances(trade_id,exchange,instrument_key,side,state) "
            "VALUES('BINANCE-ETH-USDT-PERP-LONG-20260918-001','BINANCE','ETH/USDT:PERP','LONG','PENDING_ENTRY')"
        )
        await database.execute(
            "INSERT INTO trade_instances(trade_id,exchange,instrument_key,side,state) "
            "VALUES('OKX-BTC-USDT-PERP-SHORT-20260918-002','OKX','BTC/USDT:PERP','SHORT','OPEN')"
        )
        return await command_handler.execute_reply("trades", (), "tg-trades-2")

    try:
        reply = asyncio.run(scenario())
    finally:
        asyncio.run(router.close())
    labels = _callback_button_texts(reply.keyboard)
    # PENDING_ENTRY 只允许改价与撤单；OPEN 允许平仓、取消止损、改止损。
    assert any("改挂单价" in label for label in labels)
    assert any("撤进场挂单" in label for label in labels)
    assert any("市价平仓" in label for label in labels)
    assert any("取消止损" in label for label in labels)
    assert any("改止损" in label for label in labels)
    # 未成交交易不应出现平仓按钮。
    assert not any("市价平仓" in label and "ETH" in label for label in labels)


def test_write_button_requires_confirmation(handler):
    """一键写操作必须先返回确认键盘，不能在第一次点击就执行。"""
    command_handler, database, _, router = handler
    trade_id = "BINANCE-ETH-USDT-PERP-LONG-20260918-003"

    async def scenario():
        await database.execute(
            "INSERT INTO trade_instances(trade_id,exchange,instrument_key,side,state) "
            "VALUES(?,?,'ETH/USDT:PERP','LONG','OPEN')", (trade_id, "BINANCE"),
        )
        pick = encode_callback("pick", "close_position", fingerprint(trade_id))
        return await command_handler.handle_callback(pick)

    try:
        reply = asyncio.run(scenario())
    finally:
        asyncio.run(router.close())
    labels = _callback_button_texts(reply.keyboard)
    assert any("确认执行" in label for label in labels)
    assert any("取消" in label for label in labels)
    assert "即将" in reply.text
    # 第一次点击只出确认键盘，交易编号明确展示，便于人工核对。
    assert trade_id in reply.text


def test_price_button_returns_prefilled_template(handler):
    """需要价格的指令应回填交易编号，人工只补价格。"""
    command_handler, database, _, router = handler
    trade_id = "BINANCE-ETH-USDT-PERP-LONG-20260918-004"

    async def scenario():
        await database.execute(
            "INSERT INTO trade_instances(trade_id,exchange,instrument_key,side,state) "
            "VALUES(?,?,'ETH/USDT:PERP','LONG','PENDING_ENTRY')", (trade_id, "BINANCE"),
        )
        pick = encode_callback("pick", "amend_entry", fingerprint(trade_id))
        return await command_handler.handle_callback(pick)

    try:
        reply = asyncio.run(scenario())
    finally:
        asyncio.run(router.close())
    assert f"/amend_entry {trade_id}" in reply.text
    # 模板下方仍给返回按钮，方便回到交易列表。
    assert "📋 交易与操作" in _callback_button_texts(reply.keyboard)


def test_callback_rejects_unknown_fingerprint(handler):
    """指纹查不到活动交易时必须拒绝，绝不猜测目标。"""
    command_handler, _, _, router = handler

    async def scenario():
        with pytest.raises(CommandRejected, match="不在活动列表"):
            await command_handler.handle_callback(encode_callback("go", "close_position", "deadbeef"))

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(router.close())


def test_callback_rejects_ambiguous_fingerprint(handler, monkeypatch):
    """指纹命中多笔时必须拒绝（模拟哈希碰撞）。"""
    command_handler, database, _, router = handler

    async def scenario():
        await database.execute(
            "INSERT INTO trade_instances(trade_id,exchange,instrument_key,side,state) "
            "VALUES('BINANCE-ETH-USDT-PERP-LONG-20260918-005','BINANCE','ETH/USDT:PERP','LONG','OPEN')"
        )
        await database.execute(
            "INSERT INTO trade_instances(trade_id,exchange,instrument_key,side,state) "
            "VALUES('BINANCE-ETH-USDT-PERP-LONG-20260918-006','BINANCE','ETH/USDT:PERP','LONG','OPEN')"
        )
        import telegram_commands as module
        monkeypatch.setattr(module, "fingerprint", lambda trade_id: "collide1")
        with pytest.raises(CommandRejected, match="命中多笔"):
            await command_handler.handle_callback(encode_callback("go", "close_position", "collide1"))

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(router.close())


def test_callback_rejects_malformed_data(handler):
    """非本系统的回调数据必须拒绝。"""
    command_handler, _, _, router = handler

    async def scenario():
        with pytest.raises(CommandRejected, match="无法识别"):
            await command_handler.handle_callback("evil-payload")

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(router.close())


def test_state_actions_cover_known_states():
    """状态→动作映射只包含真实命令名，避免按钮指向不存在的指令。"""
    valid = {"amend_entry", "cancel_order", "cancel_stop", "move_stop", "close_position"}
    for state, actions in STATE_ACTIONS.items():
        assert actions, state
        assert set(actions) <= valid, state


def _sample_trades():
    return [
        {"trade_id": "BINANCE-ETH-USDT-PERP-LONG-20260918-001", "state": "PENDING_ENTRY"},
        {"trade_id": "OKX-BTC-USDT-PERP-SHORT-20260918-002", "state": "OPEN"},
    ]



def test_forged_callback_cannot_run_arbitrary_command(handler):
    """伪造回调只能触发只读/人工指令白名单，不能借按钮执行任意命令。"""
    command_handler, _, _, router = handler

    async def scenario():
        with pytest.raises(CommandRejected, match="收到：danger"):
            await command_handler.handle_callback(encode_callback("run", "danger"))
        with pytest.raises(CommandRejected, match="不允许通过按钮执行"):
            await command_handler.handle_callback(encode_callback("go", "TRUNCATE", "deadbeef"))
        with pytest.raises(CommandRejected, match="不允许通过按钮执行"):
            await command_handler.handle_callback(encode_callback("pick", "TRUNCATE", "deadbeef"))

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(router.close())

def test_telegram_client_extracts_callback_query():
    """客户端必须把 callback_query 归一成 TelegramCallback。"""
    update = {
        "update_id": 42,
        "callback_query": {
            "id": "cb-1",
            "data": "cb:run:status",
            "message": {"message_id": 7, "chat": {"id": -1003795266191}},
        },
    }
    callback = TelegramClient._extract_callback(update)
    assert isinstance(callback, TelegramCallback)
    assert callback.callback_id == "cb-1"
    assert callback.chat_id == -1003795266191
    assert callback.message_id == 7
    assert callback.data == "cb:run:status"
    assert TelegramClient._extract_callback({"update_id": 1}) is None


def test_telegram_client_callback_permission_boundary():
    """按钮点击与文本命令共用权限边界：来源频道或白名单聊天。"""
    source = -1003795266191
    allowed = {source}
    admin = 5338691895
    # 来源频道放行；白名单私聊放行；未授权私聊拒绝。
    assert TelegramClient._chat_allowed(source, allowed, set()) is True
    assert TelegramClient._chat_allowed(admin, allowed, {admin}) is True
    assert TelegramClient._chat_allowed(admin, allowed, set()) is False
    assert TelegramClient._chat_allowed(999, set(), set()) is False

def _build_application(tmp_path, monkeypatch):
    settings = build_settings(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "12345:test-token")
    monkeypatch.setenv("TELEGRAM_SOURCE_CHAT_ID", "-1003795266191")
    application = Application(settings)
    application.parser = _ExplodingParser()
    application.telegram = FakeTelegram()
    return application


class _ExplodingParser:
    """命令文本绝不能进入交易解析器。"""

    async def parse(self, text, command_id):
        raise AssertionError(f"命令不应进入交易解析：{text}")


def test_handle_message_routes_command_without_parsing(tmp_path, monkeypatch):
    application = _build_application(tmp_path, monkeypatch)
    message = TelegramMessage(1, 5338691895, 1, "/status", "1789371877", False)

    try:
        asyncio.run(application._handle_message(message))
    finally:
        asyncio.run(application.router.close())

    replies = application.telegram.replies
    assert len(replies) == 1
    chat_id, text = replies[0]
    assert chat_id == 5338691895
    assert "CryptoTrade 状态" in text


def test_handle_message_replies_unknown_command(tmp_path, monkeypatch):
    application = _build_application(tmp_path, monkeypatch)
    message = TelegramMessage(2, 5338691895, 2, "/nope", "1789371877", False)

    try:
        asyncio.run(application._handle_message(message))
    finally:
        asyncio.run(application.router.close())

    chat_id, text = application.telegram.replies[0]
    assert chat_id == 5338691895
    assert "未知命令" in text


def test_handle_message_replies_manual_command_rejection(tmp_path, monkeypatch):
    """人工指令缺少有效 trade_id 时应回复拒绝原因，且不进入交易解析。"""
    application = _build_application(tmp_path, monkeypatch)
    message = TelegramMessage(4, 5338691895, 4, "/close_position NOPE", "1789371877", False)

    try:
        asyncio.run(application._handle_message(message))
    finally:
        asyncio.run(application.router.close())

    _, text = application.telegram.replies[0]
    assert "交易编号不存在" in text


def test_handle_message_deduplicates_commands(tmp_path, monkeypatch):
    application = _build_application(tmp_path, monkeypatch)
    message = TelegramMessage(3, 5338691895, 3, "/start", "1789371877", False)

    async def scenario():
        await application._handle_message(message)
        await application._handle_message(message)

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(application.router.close())
    assert len(application.telegram.replies) == 1


def test_manual_command_rejects_locked_trade(tmp_path):
    """ERROR_LOCKED（保护失败被锁）交易必须拒绝人工改写，避免在未知状态下下单。"""
    workspace = tmp_path / "locked"
    workspace.mkdir(exist_ok=True)
    settings, database, router, adapter, service, command = _build_manual_scenario(workspace)

    async def scenario():
        await database.execute(
            "INSERT INTO trade_instances(trade_id,exchange,instrument_key,side,state) "
            "VALUES('BINANCE-ETH-USDT-PERP-LONG-20260916-012','BINANCE','ETH/USDT:PERP','LONG',?)",
            ("ERROR_LOCKED",),
        )
        handler = TelegramCommandHandler(database, router, service)
        with pytest.raises(Exception, match="拒绝人工修改"):
            await handler.execute("close_position", ("BINANCE-ETH-USDT-PERP-LONG-20260916-012",), "tg-12-1")

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(router.close())

def test_amend_entry_rejected_when_remote_order_missing(tmp_path):
    """本地有进场挂单但交易所已不存在该开放订单时，改价必须拒绝而非猜测。"""
    workspace = tmp_path / "missing"
    workspace.mkdir(exist_ok=True)
    settings, database, router, adapter, service, command = _build_manual_scenario(workspace)

    async def scenario():
        await service.execute(command)
        # 模拟交易所侧挂单已消失：清空适配器中的开放订单。
        adapter.orders.clear()
        trade_id = (await database.fetch_all("SELECT trade_id FROM trade_instances"))[0]["trade_id"]
        handler = TelegramCommandHandler(database, router, service)
        with pytest.raises(Exception, match="拒绝猜测"):
            await handler.execute("amend_entry", (trade_id, "2470"), "tg-13-1")

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(router.close())


def test_close_position_rejected_when_remote_quantity_differs(tmp_path):
    """远程持仓数量与本交易进场数量不一致时必须拒绝市价平仓。"""
    from monitor import Monitor

    workspace = tmp_path / "mismatch"
    workspace.mkdir(exist_ok=True)
    settings, database, router, adapter, service, command = _build_manual_scenario(workspace)
    monitor = Monitor(router, database)

    async def scenario():
        await service.execute(command)
        await _fill_entry(database, adapter, command, monitor)
        # 远程仓位被外部改动，不再等于该交易进场数量。
        adapter.positions = [PositionSnapshot(
            Exchange.BINANCE, command.instrument_key, command.side, Decimal("9.999"), Decimal("2480"),
        )]
        trade_id = (await database.fetch_all("SELECT trade_id FROM trade_instances"))[0]["trade_id"]
        handler = TelegramCommandHandler(database, router, service)
        with pytest.raises(Exception, match="不能唯一归属"):
            await handler.execute("close_position", (trade_id,), "tg-13-2")

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(router.close())


def test_cancel_order_rejected_for_partial_fill(tmp_path):
    """部分成交时拒绝撤余单（可能已有真实持仓），只能走明确平仓路径。"""
    workspace = tmp_path / "partial"
    workspace.mkdir(exist_ok=True)
    settings, database, router, adapter, service, command = _build_manual_scenario(workspace)

    async def scenario():
        await database.execute(
            "INSERT INTO trade_instances(trade_id,exchange,instrument_key,side,state) "
            "VALUES('BINANCE-ETH-USDT-PERP-LONG-20260916-013','BINANCE','ETH/USDT:PERP','LONG',?)",
            ("PARTIAL_FILL",),
        )
        handler = TelegramCommandHandler(database, router, service)
        with pytest.raises(Exception, match="拒绝撤余单"):
            await handler.execute(
                "cancel_order", ("BINANCE-ETH-USDT-PERP-LONG-20260916-013",), "tg-13-3")

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(router.close())

def test_handle_callback_edits_message_in_place(tmp_path, monkeypatch):
    """按钮点击应就地编辑原消息、应答点击，且不进入交易公告解析。"""
    application = _build_application(tmp_path, monkeypatch)
    callback = TelegramCallback(update_id=99, callback_id="cb-99", chat_id=5338691895,
                                message_id=7, data="cb:run:status")

    try:
        asyncio.run(application._handle_callback(callback))
    finally:
        asyncio.run(application.router.close())

    assert application.telegram.answers == ["cb-99"]
    edits = application.telegram.edits
    assert len(edits) == 1
    chat_id, message_id, text, keyboard = edits[0]
    assert (chat_id, message_id) == (5338691895, 7)
    assert "CryptoTrade 状态" in text
    assert keyboard


def test_handle_callback_rejection_replies_with_menu(tmp_path, monkeypatch):
    """按钮指向已消失的交易时回复拒绝原因与主菜单，便于继续操作。"""
    application = _build_application(tmp_path, monkeypatch)
    callback = TelegramCallback(update_id=100, callback_id="cb-100", chat_id=5338691895,
                                message_id=8, data="cb:go:close_position:deadbeef")

    try:
        asyncio.run(application._handle_callback(callback))
    finally:
        asyncio.run(application.router.close())

    assert application.telegram.answers == ["cb-100"]
    _, _, text, keyboard = application.telegram.edits[0]
    assert "被拒绝" in text
    assert keyboard


def test_handle_message_start_sends_keyboard(tmp_path, monkeypatch):
    """/start 文本命令的回复必须带可点击按钮。"""
    application = _build_application(tmp_path, monkeypatch)
    message = TelegramMessage(50, 5338691895, 50, "/start", "1789371877", False)

    try:
        asyncio.run(application._handle_message(message))
    finally:
        asyncio.run(application.router.close())

    _, text = application.telegram.replies[0]
    keyboard = application.telegram.keyboards[0]
    assert "可用命令" in text
    assert keyboard
    assert "📊 运行状态" in _callback_button_texts(keyboard)
