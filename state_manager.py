"""交易状态机与唯一编号生成。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from database import Database
from models import TradeCommand, TradeState


ALLOWED_TRANSITIONS: dict[TradeState, set[TradeState]] = {
    TradeState.RECEIVED: {TradeState.PENDING_ENTRY, TradeState.REJECTED},
    TradeState.PENDING_ENTRY: {TradeState.PARTIAL_FILL, TradeState.OPEN, TradeState.CANCELLED, TradeState.ERROR_LOCKED},
    TradeState.PARTIAL_FILL: {TradeState.OPEN, TradeState.CANCELLED, TradeState.ERROR_LOCKED},
    # OPEN 同时保留 CLOSED 与 CLOSING 两条出边：
    # - CLOSED：止盈/止损在交易所被触发（保护单成交回报直接终结交易，不经 CLOSING）；
    # - CLOSING：人工市价平仓已受理，等待平仓单成交回报后收敛为 CLOSED。
    TradeState.OPEN: {TradeState.WAITING_ADD, TradeState.CLOSING, TradeState.CLOSED, TradeState.ERROR_LOCKED},
    TradeState.WAITING_ADD: {TradeState.OPEN, TradeState.CLOSED, TradeState.ERROR_LOCKED},
    TradeState.CLOSING: {TradeState.CLOSED, TradeState.ERROR_LOCKED},
}


class StateManager:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create_trade(self, command: TradeCommand) -> str:
        date = datetime.now(ZoneInfo("Asia/Singapore")).strftime("%Y%m%d")
        prefix = f"{command.exchange.value}-{command.base_asset}-USDT-PERP-{command.side.value}-{date}"
        rows = await self.database.fetch_all(
            "SELECT trade_id FROM trade_instances WHERE trade_id LIKE ? ORDER BY trade_id DESC LIMIT 1",
            (prefix + "-%",),
        )
        sequence = int(rows[0]["trade_id"].rsplit("-", 1)[1]) + 1 if rows else 1
        trade_id = f"{prefix}-{sequence:03d}"
        await self.database.execute(
            "INSERT INTO trade_instances(trade_id,exchange,instrument_key,side,state) VALUES(?,?,?,?,?)",
            (trade_id, command.exchange.value, command.instrument_key, command.side.value, TradeState.RECEIVED.value),
        )
        return trade_id

    async def transition(self, trade_id: str, target: TradeState) -> None:
        rows = await self.database.fetch_all("SELECT state FROM trade_instances WHERE trade_id=?", (trade_id,))
        if not rows:
            raise ValueError(f"交易不存在：{trade_id}")
        current = TradeState(rows[0]["state"])
        if target not in ALLOWED_TRANSITIONS.get(current, set()):
            raise ValueError(f"非法状态转换：{current.value} -> {target.value}")
        await self.database.execute(
            "UPDATE trade_instances SET state=?,updated_at=CURRENT_TIMESTAMP WHERE trade_id=?",
            (target.value, trade_id),
        )
        await self.database.audit("STATE_TRANSITION", trade_id, "SUCCESS", current.value, target.value)

    async def lock(self, trade_id: str, reason: str) -> None:
        await self.database.execute(
            "UPDATE trade_instances SET state=?,updated_at=CURRENT_TIMESTAMP WHERE trade_id=?",
            (TradeState.ERROR_LOCKED.value, trade_id),
        )
        await self.database.audit("STATE_MISMATCH", trade_id, reason)
