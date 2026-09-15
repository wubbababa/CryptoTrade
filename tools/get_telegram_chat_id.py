"""列出 Telegram Bot 最近收到消息的 Chat ID，不确认或删除这些更新。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import aiohttp


def load_token() -> str:
    """只从本地 .env 读取 Token，不输出密钥。"""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            if raw_line.startswith("TELEGRAM_BOT_TOKEN="):
                os.environ.setdefault("TELEGRAM_BOT_TOKEN", raw_line.split("=", 1)[1].strip())
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(".env 中缺少 TELEGRAM_BOT_TOKEN")
    return token


async def main() -> None:
    token = load_token()
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.get(url, params={
            "timeout": 0,
            "allowed_updates": '["message","channel_post","edited_channel_post"]',
        }) as response:
            payload = await response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API 错误：{payload.get('description')}")
    chats: dict[int, tuple[str, str]] = {}
    for update in payload["result"]:
        message = update.get("channel_post") or update.get("edited_channel_post") or update.get("message")
        if not message:
            continue
        chat = message["chat"]
        chats[int(chat["id"])] = (str(chat.get("title") or chat.get("username") or "未命名"), str(chat.get("type", "")))
    if not chats:
        print("没有可用更新。请将 Bot 加为频道管理员，并在频道发布一条新的测试消息后重试。")
        return
    print("Bot 最近收到消息的聊天来源：")
    for chat_id, (title, chat_type) in chats.items():
        print(f"Chat ID: {chat_id} | 类型: {chat_type} | 名称: {title}")


if __name__ == "__main__":
    asyncio.run(main())
