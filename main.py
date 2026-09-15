"""CryptoTrade 程序入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal

from app_logging import configure_logging
from database import Database
from deepseek_parser import DeepSeekParser
from dev_runner import run_dev_test
from exchange_router import ExchangeRouter
from monitor import Monitor
from settings import Settings
from telegram_client import TelegramClient
from trading_service import TradingService

logger = logging.getLogger(__name__)


class Application:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database = Database(settings.database_path)
        self.database.initialize()
        self.router = ExchangeRouter(settings)
        self.monitor = Monitor(self.router, self.database)
        # 默认使用 DeepSeek API 解析器进行 Telegram 自然语言交易信号识别
        self.parser = DeepSeekParser(settings)
        self.service = TradingService(settings, self.database, self.router)
        self.telegram = TelegramClient(settings.raw.get("telegram", {}), self.database)
        self.stop_event = asyncio.Event()
        self.monitor_tasks: list[asyncio.Task] = []

    async def run(self) -> None:
        if not self.router.adapters:
            raise RuntimeError("没有可用交易所；请检查官方测试环境 API 凭据和 mode 配置")
        warnings = await self.monitor.reconcile()
        for warning in warnings:
            logger.critical(warning)
        if not self.router.adapters:
            raise RuntimeError("所有交易所均在启动对账中失败，系统已停止，未接收 Telegram 指令")
        self.monitor_tasks = self.monitor.tasks()
        if not self.settings.raw.get("telegram", {}).get("enabled", False):
            logger.warning("Telegram 未启用；系统仅完成启动对账。按 Ctrl+C 退出。")
            await self.stop_event.wait()
            return
        await self.telegram.start()
        async for message in self.telegram.messages():
            if self.stop_event.is_set():
                break
            asyncio.create_task(self._handle_message(message), name=f"tg-{message.chat_id}-{message.message_id}")

    async def _handle_message(self, message) -> None:
        # 先以主键去重，确保同一消息不会重复下单。
        inserted = await self.database.execute(
            "INSERT OR IGNORE INTO telegram_messages(chat_id,message_id,raw_text,received_at) VALUES(?,?,?,?)",
            (message.chat_id, message.message_id, message.text, message.received_at),
        )
        if inserted == 0:
            logger.info("忽略重复 Telegram 消息 %s/%s", message.chat_id, message.message_id)
            return
        command_id = f"tg-{message.chat_id}-{message.message_id}"
        try:
            command = await self.parser.parse(message.text, command_id)
            await self.database.execute(
                "UPDATE telegram_messages SET parsed_json=? WHERE chat_id=? AND message_id=?",
                (json.dumps(command, default=lambda value: getattr(value, "value", str(value)), ensure_ascii=False),
                 message.chat_id, message.message_id),
            )
            report = await self.service.execute(command)
        except Exception as exc:
            report = f"拒绝执行 {command_id}：{exc}"
            await self.database.audit("COMMAND_REJECTED", command_id, str(exc))
            logger.exception("处理消息失败")
        # 来源频道通常不允许 Bot 发言；执行回报使用独立目标，未配置时仅写日志。
        await self.telegram.report(report)

    async def shutdown(self) -> None:
        logger.warning("正在停止接收新指令；交易所端现有订单不会撤销。")
        self.stop_event.set()
        await self.telegram.close()
        for task in self.monitor_tasks:
            task.cancel()
        await asyncio.gather(*self.monitor_tasks, return_exceptions=True)
        await self.router.close()
        logger.warning("程序已退出，本地动态保本监控已经停止。")


async def async_main(config_path: str) -> None:
    settings = Settings.load(config_path)
    configure_logging(settings.log_path)
    app = Application(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, app.stop_event.set)
        except NotImplementedError:
            # Windows ProactorEventLoop 不支持 add_signal_handler，KeyboardInterrupt 仍可正常处理。
            pass
    try:
        await app.run()
    finally:
        await app.shutdown()



def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram 多交易所自动交易系统")
    parser.add_argument("--config", default="config.yaml", help="YAML 配置文件路径")
    parser.add_argument("--dev", action="store_true", help="开发者测试模式：调用 DeepSeek API 打印解析结果并执行模拟下单")
    parser.add_argument("--signal", default=None, help="自定义待测试公告文本（可选，配合 --dev 使用）")
    args = parser.parse_args()
    try:
        if args.dev:
            asyncio.run(run_dev_test(args.config, args.signal))
        else:
            asyncio.run(async_main(args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

