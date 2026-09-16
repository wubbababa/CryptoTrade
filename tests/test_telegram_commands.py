"""Telegram 命令层测试：解析、回复内容与消息分发。"""

import asyncio

import pytest
import yaml

from database import Database
from exchange_router import ExchangeRouter
from main import Application
from settings import Settings
from telegram_client import TelegramMessage, _optional_chat_id_list
from telegram_commands import TelegramCommandHandler, UnknownCommand, _truncate, parse_command


def build_settings(tmp_path, mode="LOCAL"):
    config_path = tmp_path / "config.yaml"
    source = Settings.load("config.yaml").raw
    for exchange_config in source["exchanges"].values():
        exchange_config["mode"] = mode
    config_path.write_text(yaml.safe_dump(source, allow_unicode=True), encoding="utf-8")
    return Settings.load(config_path)


class FakeTelegram:
    """记录 reply/report 调用，不访问网络。"""

    def __init__(self) -> None:
        self.replies: list[tuple[int, str]] = []
        self.reports: list[str] = []
        self.bot_username = "Crypto20260909_bot"

    async def reply(self, chat_id: int, text: str) -> None:
        self.replies.append((chat_id, text))

    async def report(self, text: str) -> None:
        self.reports.append(text)


@pytest.fixture
def handler(tmp_path):
    settings = build_settings(tmp_path)
    database = Database(tmp_path / "trading.db")
    database.initialize()
    router = ExchangeRouter(settings)
    return TelegramCommandHandler(database, router), database


def test_parse_command_plain_and_with_args():
    assert parse_command("/start") == ("start", ())
    assert parse_command("  /status  ") == ("status", ())
    assert parse_command("/help extra arg") == ("help", ("extra", "arg"))


def test_parse_command_ignores_non_commands_and_other_bots():
    assert parse_command("ETH2480附近多") is None
    assert parse_command("") is None
    # 已知自身用户名时，@其他机器人必须忽略。
    assert parse_command("/status@other_bot", "Crypto20260909_bot") is None
    # 未知自身用户名时无法判断归属，宽松放行并剥掉后缀。
    assert parse_command("/status@other_bot") == ("status", ())


def test_parse_command_accepts_own_username():
    assert parse_command("/status@Crypto20260909_bot", "Crypto20260909_bot") == ("status", ())
    assert parse_command("/STATUS", "Crypto20260909_bot") == ("status", ())


def test_command_chat_id_list_parsing(monkeypatch):
    assert _optional_chat_id_list("-1001234567890, 5338691895") == [-1001234567890, 5338691895]
    # 拒绝把邀请链接当作命令来源。
    with pytest.raises(RuntimeError, match="邀请链接"):
        _optional_chat_id_list("https://t.me/+example")


def test_handler_replies_start_and_help(handler):
    command_handler, _ = handler

    async def scenario():
        return await command_handler.execute("start"), await command_handler.execute("help")

    start_text, help_text = asyncio.run(scenario())
    assert "/status" in start_text
    assert start_text == help_text


def test_handler_rejects_unknown_command(handler):
    command_handler, _ = handler

    async def scenario():
        with pytest.raises(UnknownCommand):
            await command_handler.execute("rm-rf")

    asyncio.run(scenario())


def test_handler_status_reports_trades_and_equity(handler):
    command_handler, database = handler

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

    text = asyncio.run(scenario())
    assert "PENDING_ENTRY" in text
    assert "OKX-ETH-USDT-PERP-LONG-20260916-001" in text
    assert "CLOSED" not in text.split("【活动交易】")[1].split("【")[0]
    assert "权益" in text


def test_handler_status_survives_broken_exchange(handler):
    command_handler, _ = handler
    # 让适配器读取权益时抛错，状态查询仍应返回文本。
    for adapter in command_handler.router.adapters.values():
        async def boom():
            raise RuntimeError("network down")

        adapter.get_equity = boom

    async def scenario():
        return await command_handler.execute("status")

    text = asyncio.run(scenario())
    assert "读取失败" in text


def test_truncate_keeps_short_text_and_marks_long_text():
    assert _truncate("短文本") == "短文本"
    truncated = _truncate("x" * 5000, limit=100)
    assert len(truncated) <= 100
    assert "截断" in truncated


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

    asyncio.run(application._handle_message(message))

    replies = application.telegram.replies
    assert len(replies) == 1
    chat_id, text = replies[0]
    assert chat_id == 5338691895
    assert "CryptoTrade 状态" in text


def test_handle_message_replies_unknown_command(tmp_path, monkeypatch):
    application = _build_application(tmp_path, monkeypatch)
    message = TelegramMessage(2, 5338691895, 2, "/nope", "1789371877", False)

    asyncio.run(application._handle_message(message))

    chat_id, text = application.telegram.replies[0]
    assert chat_id == 5338691895
    assert "未知命令" in text


def test_handle_message_deduplicates_commands(tmp_path, monkeypatch):
    application = _build_application(tmp_path, monkeypatch)
    message = TelegramMessage(3, 5338691895, 3, "/start", "1789371877", False)

    async def scenario():
        await application._handle_message(message)
        await application._handle_message(message)

    asyncio.run(scenario())
    assert len(application.telegram.replies) == 1