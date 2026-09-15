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


class TelegramClient:
    def __init__(self, config: dict, database: Database) -> None:
        self.config = config
        self.database = database
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.source_chat_id = _optional_chat_id(os.getenv("TELEGRAM_SOURCE_CHAT_ID", ""))
        self.report_chat_id = _optional_chat_id(os.getenv("TELEGRAM_REPORT_CHAT_ID", ""))
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.session: aiohttp.ClientSession | None = None
        self.offset: int | None = None
        self.running = True

    async def start(self) -> None:
        if not self.token:
            raise RuntimeError("Telegram 已启用但缺少 TELEGRAM_BOT_TOKEN")
        if self.source_chat_id is None:
            raise RuntimeError(
                "Telegram 已启用但缺少 TELEGRAM_SOURCE_CHAT_ID；"
                "请先运行 python tools/get_telegram_chat_id.py 获取"
            )
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))
        rows = await self.database.fetch_all("SELECT value FROM runtime_state WHERE key='telegram_offset'")
        if rows:
            self.offset = int(rows[0]["value"])
        else:
            # 第一次启动仅取得最新 update，并从其后开始，避免执行历史信号。
            updates = await self._get_updates(offset=-1, timeout=0)
            self.offset = (updates[-1]["update_id"] + 1) if updates else 0
            await self._save_offset()

    async def messages(self) -> AsyncIterator[TelegramMessage]:
        if self.session is None:
            await self.start()
        # 只接收明确配置的来源频道，避免其他聊天消息触发交易。
        allowed = {self.source_chat_id}
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
                    if allowed and chat_id not in allowed:
                        continue
                    yield TelegramMessage(update["update_id"], chat_id, int(message["message_id"]),
                                          message["text"], str(message.get("date", "")))
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("Telegram 轮询暂时失败：%s", exc)
                await asyncio.sleep(3)

    async def _get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        assert self.session is not None
        async with self.session.get(f"{self.base_url}/getUpdates", params={
            "offset": offset, "timeout": timeout, "allowed_updates": '["message","channel_post"]',
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
        async with self.session.post(f"{self.base_url}/sendMessage", json={"chat_id": target, "text": text}) as response:
            payload = await response.json()
            if not payload.get("ok"):
                logger.error("发送 Telegram 回报失败：%s", payload.get("description"))

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
