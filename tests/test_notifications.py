"""运行通知测试。"""

import asyncio

from database import Database
from notifications import EventNotifier


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def report(self, text: str) -> None:
        self.messages.append(text)


def test_notification_is_audited_and_sent(tmp_path):
    async def scenario():
        database = Database(tmp_path / "trading.db")
        database.initialize()
        telegram = FakeTelegram()
        notifier = EventNotifier(database, telegram, {"enabled": True})
        await notifier.emit("CRITICAL", "PROTECTION_FAILED", "保护单创建失败", "trade-1", {"exchange": "BINANCE"})
        audits = await database.fetch_all("SELECT category,result,subject_id FROM audit_logs")
        return telegram.messages, audits

    messages, audits = asyncio.run(scenario())
    assert "🚨 [PROTECTION_FAILED]" in messages[0]
    assert "交易：trade-1" in messages[0]
    assert audits == [{"category": "NOTIFY_PROTECTION_FAILED", "result": "CRITICAL", "subject_id": "trade-1"}]


def test_notification_level_can_be_disabled(tmp_path):
    async def scenario():
        database = Database(tmp_path / "trading.db")
        database.initialize()
        telegram = FakeTelegram()
        notifier = EventNotifier(database, telegram, {"enabled": True, "send_info": False})
        await notifier.emit("INFO", "STARTED", "程序已启动")
        audits = await database.fetch_all("SELECT category FROM audit_logs")
        return telegram.messages, audits

    messages, audits = asyncio.run(scenario())
    assert messages == []
    assert audits == [{"category": "NOTIFY_STARTED"}]
