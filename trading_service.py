"""交易用例编排层：校验、风险、路由、落库和审计。"""

from __future__ import annotations

import json
import hashlib
from dataclasses import asdict, replace
from decimal import Decimal

from database import Database
from exchange_router import ExchangeRouter
from models import CommandType, OrderRequest, PositionSide, TradeCommand, TradeState
from risk_manager import RiskManager
from position_sizer import PositionSizer
from settings import Settings
from state_manager import StateManager
from validator import CommandValidator


class TradingService:
    def __init__(self, settings: Settings, database: Database, router: ExchangeRouter) -> None:
        self.settings = settings
        self.database = database
        self.router = router
        self.validator = CommandValidator(settings)
        self.risk = RiskManager(settings)
        self.position_sizer = PositionSizer(settings)
        self.states = StateManager(database)

    async def execute(self, command: TradeCommand) -> str:
        self.validator.validate(command)
        payload = json.dumps(asdict(command), ensure_ascii=False, default=str)
        inserted = await self.database.execute(
            "INSERT OR IGNORE INTO commands(command_id,trade_id,command_type,payload_json,status) VALUES(?,?,?,?,?)",
            (command.command_id, command.trade_id, command.command_type.value, payload, "RECEIVED"),
        )
        if inserted == 0:
            return f"忽略重复指令 {command.command_id}"
        if command.command_type != CommandType.OPEN_POSITION:
            raise NotImplementedError("第一版执行器目前只开放安全的开仓流程，修改指令已拒绝")
        adapter = self.router.get(command.exchange)
        equity = await adapter.get_equity()
        if equity <= 0:
            raise ValueError(f"{command.exchange.value} 账户权益必须大于零")
        instruments = await adapter.load_instruments()
        instrument = instruments.get(command.base_asset)
        if instrument is None:
            raise ValueError(f"{command.exchange.value} 不支持 {command.instrument_key}")
        margin_mode = self.settings.exchange_config(command.exchange).get("margin_mode", "CROSS")
        requested_leverage = Decimal(str(self.settings.raw["trading"]["leverage"]))
        leverage = await adapter.resolve_leverage(instrument, requested_leverage, margin_mode)
        command = replace(command, quantity=self.position_sizer.calculate(command, equity, leverage))
        self.risk.check_open(command, equity)
        return await self._open(command, instrument, leverage, margin_mode)

    async def _open(self, command: TradeCommand, instrument, leverage: Decimal,
                    margin_mode: str) -> str:
        assert command.entry is not None and command.quantity is not None
        trade_id = await self.states.create_trade(command)
        adapter = self.router.get(command.exchange)
        instruments = await adapter.load_instruments()
        instrument = instruments.get(command.base_asset)
        if instrument is None:
            await self.states.transition(trade_id, TradeState.REJECTED)
            raise ValueError(f"{command.exchange.value} 不支持 {command.instrument_key}")
        entry_price = command.entry.low if command.side == PositionSide.LONG else command.entry.high
        client_id = self._client_order_id(command, "ENTRY")
        tp_price = command.take_profits[0] if command.take_profits else None
        sl_price = command.stop_loss
        request = OrderRequest(
            instrument=instrument,
            position_side=command.side,
            order_side="BUY" if command.side == PositionSide.LONG else "SELL",
            order_type="LIMIT", quantity=command.quantity, price=entry_price,
            reduce_only=False, client_order_id=client_id,
            margin_mode=margin_mode,
            leverage=leverage,
            take_profit_price=tp_price,
            stop_loss_price=sl_price,
        )
        try:
            # 先持久化可关联的客户订单号，再请求交易所，防止成交推送先于 HTTP 回包到达。
            await self.database.execute(
                "INSERT INTO orders(trade_id,exchange_order_id,client_order_id,order_type,price,quantity,status) "
                "VALUES(?,?,?,?,?,?,?)",
                (trade_id, None, client_id, "ENTRY", str(entry_price), str(command.quantity), "SUBMITTING"),
            )
            await self.database.execute(
                "UPDATE commands SET trade_id=?,status='SUBMITTING' WHERE command_id=?",
                (trade_id, command.command_id),
            )
            result = await adapter.place_entry_order(request)
            await self.database.execute(
                "UPDATE orders SET exchange_order_id=?,status=? WHERE client_order_id=?",
                (result.exchange_order_id, result.status, client_id),
            )
            await self.database.execute("UPDATE commands SET trade_id=?,status='EXECUTED' WHERE command_id=?",
                                        (trade_id, command.command_id))
            await self.states.transition(trade_id, TradeState.PENDING_ENTRY)
            await self.database.audit("PLACE_ENTRY", trade_id, "SUCCESS", after=result.raw)
        except Exception as exc:
            await self.database.execute("UPDATE orders SET status='REJECTED' WHERE client_order_id=?", (client_id,))
            await self.database.execute("UPDATE commands SET status='REJECTED' WHERE command_id=?", (command.command_id,))
            await self.states.transition(trade_id, TradeState.REJECTED)
            await self.database.audit("PLACE_ENTRY", trade_id, f"FAILED: {exc}")
            raise
        note = "（公告未指定交易所，已使用本地默认值）" if command.exchange_defaulted else ""
        return f"已提交 {trade_id} 进场挂单至 {command.exchange.value}{note}，订单号 {result.exchange_order_id}"

    @staticmethod
    def _client_order_id(command: TradeCommand, suffix: str) -> str:
        # 保持较短以兼容不同交易所的客户端订单号长度限制。
        # OKX 仅允许字母数字；摘要包含完整消息编号、交易所及订单用途，避免截断碰撞。
        identity = f"{command.exchange.value}:{command.command_id}:{suffix}"
        return "ct" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:26]
