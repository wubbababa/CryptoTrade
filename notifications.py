"""运行通知与审计摘要。"""

from __future__ import annotations

import logging
from typing import Any

from database import Database

logger = logging.getLogger(__name__)


class EventNotifier:
    """把关键运行事件同时写入审计表、日志和可选 Telegram 回报。"""

    def __init__(self, database: Database, telegram, config: dict | None = None) -> None:
        self.database = database
        self.telegram = telegram
        self.config = config or {}

    async def emit(self, level: str, event: str, message: str, subject_id: str | None = None,
                   details: dict[str, Any] | None = None) -> None:
        level = level.upper()
        await self.database.audit(f"NOTIFY_{event}", subject_id, level, after=details)
        getattr(logger, level.lower(), logger.info)("[%s] %s%s", event, message,
                                                    f" subject={subject_id}" if subject_id else "")
        if not self.config.get("enabled", True) or not self._enabled_for(level):
            return
        prefix = {"INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "❌", "CRITICAL": "🚨"}.get(level, "•")
        subject = f"\n交易：{subject_id}" if subject_id else ""
        await self.telegram.report(f"{prefix} [{event}]\n{message}{subject}")

    def _enabled_for(self, level: str) -> bool:
        key = {"INFO": "send_info", "WARNING": "send_warnings", "ERROR": "send_warnings",
               "CRITICAL": "send_critical"}.get(level, "send_info")
        return bool(self.config.get(key, True))
