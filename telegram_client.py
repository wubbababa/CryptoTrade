"""Telegram Bot API 长轮询客户端。启动时跳过历史积压。"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass

import aiohttp

from database import Database

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TelegramMessage:
    update_id: int
    chat_id: int
    message_id: int
    text: str
    received_at: str
    # 是否来自配置的交易公告来源频道；命令来源（如私聊）为 False。
    is_source: bool = False


class TelegramClient:
    def __init__(self, config: dict, database: Database) -> None:
        self.config = config
        self.database = database
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.source_chat_id = _optional_chat_id(os.getenv("TELEGRAM_SOURCE_CHAT_ID", ""))
        self.report_chat_id = _optional_chat_id(os.getenv("TELEGRAM_REPORT_CHAT_ID", ""))
        # 允许发送 /start、/status 等只读命令的聊天；不配置时仅来源频道可用。
        raw_command_chats = (
            os.getenv("TELEGRAM_COMMAND_CHAT_IDS", "") or os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
        )
        self.command_chat_ids = _optional_chat_id_list(raw_command_chats)
        self.bot_username: str | None = None
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.session: aiohttp.ClientSession | None = None
        self.offset: int | None = None
        self.running = True

    async def start(self) -> None:
        if self.session is not None and not self.session.closed:
            return
        if not self.token:
            raise RuntimeError("Telegram 已启用但缺少 TELEGRAM_BOT_TOKEN")
        if self.source_chat_id is None:
            raise RuntimeError(
                "Telegram 已启用但缺少 TELEGRAM_SOURCE_CHAT_ID；"
                "请先运行 python tools/get_telegram_chat_id.py 获取"
            )
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))
        await self._load_bot_username()
        rows = await self.database.fetch_all("SELECT value FROM runtime_state WHERE key='telegram_offset'")
        if rows:
            self.offset = int(rows[0]["value"])
        else:
            # 第一次启动仅取得最新 update，并从其后开始，避免执行历史信号。
            updates = await self._get_updates(offset=-1, timeout=0)
            self.offset = (updates[-1]["update_id"] + 1) if updates else 0
            await self._save_offset()

    async def _load_bot_username(self) -> None:
        """获取 Bot 用户名，用于识别 /status@本机器人 这类命令。"""
        try:
            async with self.session.get(f"{self.base_url}/getMe") as response:
                payload = await response.json()
            if payload.get("ok"):
                self.bot_username = payload["result"].get("username")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            # 用户名只影响 @机器人 后缀的命令，失败不阻断启动。
            logger.warning("获取 Bot 用户名失败：%s", exc)

    async def messages(self) -> AsyncIterator[TelegramMessage]:
        if self.session is None:
            await self.start()
        # 来源频道：所有文本都可能是交易公告。
        # 命令聊天（如管理员私聊）：只接收 / 开头的文本，不解析为交易公告。
        allowed = {self.source_chat_id}
        command_allowed = set(self.command_chat_ids)
        while self.running:
            try:
                updates = await self._get_updates(self.offset, int(self.config.get("poll_timeout_seconds", 30)))
                for update in updates:
                    self.offset = update["update_id"] + 1
                    await self._save_offset()
                    message = update.get("channel_post") or update.get("message")
                    if not message or not message.get("text"):
                        continue
                    chat_id = int(message["chat"]["id"])
                    is_source = allowed and chat_id in allowed
                    if not is_source:
                        # 非来源频道仅在显式允许且是命令时才放行。
                        if chat_id not in command_allowed or not message["text"].strip().startswith("/"):
                            continue
                    yield TelegramMessage(update["update_id"], chat_id, int(message["message_id"]),
                                          message["text"], str(message.get("date", "")), is_source)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("Telegram 轮询暂时失败：%s", exc)
                await asyncio.sleep(3)
            except RuntimeError as exc:
                # Telegram 返回非 ok（例如残留 webhook 导致 409）时退避重试，不终止整个程序。
                logger.error("Telegram 轮询被拒绝：%s", exc)
                await asyncio.sleep(5)

    async def _get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        assert self.session is not None
        async with self.session.get(f"{self.base_url}/getUpdates", params={
            "offset": offset, "timeout": timeout,
            "allowed_updates": '["message","channel_post","edited_channel_post"]',
        }) as response:
            payload = await response.json()
            if not payload.get("ok"):
                raise RuntimeError(f"Telegram API 错误：{payload.get('description')}")
            return payload["result"]

    async def report(self, text: str, chat_id: int | None = None) -> None:
        target = chat_id or self.report_chat_id or self.config.get("report_chat_id")
        if target is None or self.session is None:
            logger.info("Telegram 回报：%s", text)
            return
        try:
            async with self.session.post(f"{self.base_url}/sendMessage", json={"chat_id": target, "text": text}) as response:
                payload = await response.json()
                if not payload.get("ok"):
                    logger.error("发送 Telegram 回报失败：%s", payload.get("description"))
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.error("发送 Telegram 回报时网络失败：%s", exc)

    async def reply(self, chat_id: int, text: str) -> None:
        """把命令回复发回请求所在的聊天；未配置回报目标也照常发送。"""
        if self.session is None:
            logger.info("Telegram 回复（未发送）：%s", text)
            return
        try:
            async with self.session.post(f"{self.base_url}/sendMessage",
                                         json={"chat_id": chat_id, "text": _truncate_for_telegram(text)}) as response:
                payload = await response.json()
                if not payload.get("ok"):
                    # 频道里 Bot 无发言权时退化为日志，避免命令处理因此失败。
                    logger.error("发送 Telegram 回复失败：%s", payload.get("description"))
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.error("发送 Telegram 回复时网络失败：%s", exc)

    async def _save_offset(self) -> None:
        await self.database.execute(
            "INSERT INTO runtime_state(key,value) VALUES('telegram_offset',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(self.offset),),
        )

    async def close(self) -> None:
        self.running = False
        if self.offset is not None:
            await self._save_offset()
        if self.session:
            await self.session.close()


def _optional_chat_id(value: str) -> int | str | None:
    """解析数字 Chat ID 或 @公开用户名，并拒绝误填邀请链接。"""
    value = value.strip()
    if not value:
        return None
    if value.startswith("https://t.me/+") or value.startswith("t.me/+"):
        raise RuntimeError("Telegram 邀请链接不能作为 Chat ID，请填写 -100... 格式的数字 ID")
    if value.startswith("@"):
        return value
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError("Telegram Chat ID 必须是 -100... 数字或 @公开用户名") from exc


def _optional_chat_id_list(value: str) -> list[int | str]:
    """解析逗号分隔的 Chat ID 列表；空白项忽略。"""
    result: list[int | str] = []
    for item in value.split(","):
        if not item.strip():
            continue
        parsed = _optional_chat_id(item)
        if parsed is not None and parsed not in result:
            result.append(parsed)
    return result


def _truncate_for_telegram(text: str, limit: int = 4000) -> str:
    """Telegram 单条消息上限 4096 字符，超出时安全截断。"""
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n…（内容已截断）"
