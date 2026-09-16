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
            try:
                report = await self._amend(command)
                await self.database.execute("UPDATE commands SET status='EXECUTED' WHERE command_id=?", (command.command_id,))
                return report
            except Exception as exc:
                await self.database.execute("UPDATE commands SET status='REJECTED' WHERE command_id=?", (command.command_id,))
                await self.database.audit("AMENDMENT", command.trade_id, f"FAILED: {exc}")
                raise
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

    async def _amend(self, command: TradeCommand) -> str:
        """执行人工指令；所有操作先验证 trade_id 与远程订单/仓位的唯一对应关系。"""
        assert command.trade_id is not None
        trade = await self._load_trade(command)
        adapter = self.router.get(command.exchange)
        instruments = await adapter.load_instruments()
        instrument = instruments.get(command.base_asset)
        if instrument is None:
            raise ValueError(f"{command.exchange.value} 不支持 {command.instrument_key}")
        if command.command_type == CommandType.AMEND_ENTRY:
            return await self._amend_entry(command, trade, adapter, instrument)
        if command.command_type == CommandType.MOVE_STOP:
            return await self._move_stop(command, trade, adapter, instrument)
        if command.command_type == CommandType.CANCEL_ORDER:
            return await self._cancel_by_state(command, trade, adapter)
        if command.command_type == CommandType.CLOSE_POSITION:
            return await self._close_position(command, trade, adapter, instrument)
        raise ValueError(f"不支持的人工指令 {command.command_type.value}")

    async def _load_trade(self, command: TradeCommand) -> dict:
        rows = await self.database.fetch_all(
            "SELECT trade_id,exchange,instrument_key,side,state FROM trade_instances WHERE trade_id=?",
            (command.trade_id,),
        )
        if not rows:
            raise ValueError(f"交易编号不存在：{command.trade_id}")
        trade = rows[0]
        if (trade["exchange"], trade["instrument_key"], trade["side"]) != (
            command.exchange.value, command.instrument_key, command.side.value,
        ):
            raise ValueError("指令的交易所、合约或方向与交易编号不一致")
        if trade["state"] in {TradeState.ERROR_LOCKED.value, TradeState.CLOSED.value, TradeState.CANCELLED.value}:
            raise ValueError(f"交易当前状态 {trade['state']}，拒绝人工修改")
        return trade

    async def _remote_order(self, adapter, trade_id: str, order_type: str) -> tuple[dict, object]:
        rows = await self.database.fetch_all(
            "SELECT id,exchange_order_id,client_order_id,price,quantity FROM orders WHERE trade_id=? AND order_type=? "
            "AND status IN ('NEW','OPEN','PARTIALLY_FILLED') ORDER BY id DESC LIMIT 1", (trade_id, order_type),
        )
        if not rows:
            raise ValueError(f"未找到活动 {order_type} 本地订单")
        local = rows[0]
        remote = next((item for item in await adapter.get_open_orders()
                       if item.client_order_id == local["client_order_id"]), None)
        if remote is None:
            raise ValueError(f"活动 {order_type} 未在交易所开放订单中，拒绝猜测")
        return local, remote

    async def _amend_entry(self, command: TradeCommand, trade: dict, adapter, instrument) -> str:
        if trade["state"] != TradeState.PENDING_ENTRY.value:
            raise ValueError("只有未成交进场挂单可以改价")
        assert command.entry is not None
        local, remote = await self._remote_order(adapter, trade["trade_id"], "ENTRY")
        new_price = command.entry.low if command.side == PositionSide.LONG else command.entry.high
        request = OrderRequest(instrument, command.side, "BUY" if command.side == PositionSide.LONG else "SELL",
                               "LIMIT", Decimal(local["quantity"]), new_price, False,
                               local["client_order_id"])
        result = await adapter.amend_entry_order(remote.exchange_order_id, request)
        await self.database.execute("UPDATE orders SET exchange_order_id=?,price=?,status=? WHERE id=?",
                                    (result.exchange_order_id, str(new_price), result.status.upper(), local["id"]))
        await self.database.audit("AMEND_ENTRY", trade["trade_id"], "SUCCESS", before={"price": local["price"]},
                                  after={"price": str(new_price), "order_id": result.exchange_order_id})
        return f"{trade['trade_id']} 进场挂单已改为 {new_price}，订单号 {result.exchange_order_id}"

    async def _move_stop(self, command: TradeCommand, trade: dict, adapter, instrument) -> str:
        if trade["state"] not in {TradeState.OPEN.value, TradeState.WAITING_ADD.value}:
            raise ValueError("只有已开仓或等待恢复止损的交易可以修改止损")
        local = None
        if trade["state"] == TradeState.OPEN.value:
            local, _ = await self._remote_order(adapter, trade["trade_id"], "STOP_LOSS")
        positions = await adapter.get_positions()
        position = next((item for item in positions if item.instrument_key == trade["instrument_key"]
                         and item.side.value == trade["side"]), None)
        if position is None or position.quantity <= 0:
            raise ValueError("交易所未找到对应持仓")
        if trade["state"] == TradeState.WAITING_ADD.value:
            trade_quantity = await self._filled_quantity(trade["trade_id"])
            if trade_quantity is None or position.quantity != trade_quantity:
                raise ValueError("远程汇总仓位不能唯一归属此交易，拒绝恢复止损")
        new_price = command.stop_loss
        if new_price is None:
            new_price = position.average_price * (Decimal("1.01") if command.side == PositionSide.LONG else Decimal("0.99"))
        if local is not None:
            old_price = Decimal(str(local["price"]))
            if (command.side == PositionSide.LONG and new_price < old_price) or (
                command.side == PositionSide.SHORT and new_price > old_price
            ):
                raise ValueError("止损只能向有利方向移动；取消止损后恢复请进入 WAITING_ADD 流程")
        request = OrderRequest(instrument, command.side, "SELL" if command.side == PositionSide.LONG else "BUY",
                               "MARKET", Decimal(local["quantity"]) if local else trade_quantity, new_price, True,
                               self._client_order_id(command, "MOVE_STOP"))
        result = await adapter.place_stop_loss(request)
        if local:
            try:
                await adapter.cancel_order(local["exchange_order_id"])
            except Exception:
                await adapter.cancel_order(result.exchange_order_id)
                raise
            await self.database.execute("UPDATE orders SET status='CANCELED' WHERE id=?", (local["id"],))
        await self.database.execute(
            "INSERT INTO orders(trade_id,exchange_order_id,client_order_id,order_type,price,quantity,status) VALUES(?,?,?,?,?,?,?)",
            (trade["trade_id"], result.exchange_order_id, result.client_order_id, "STOP_LOSS", str(new_price),
             str(request.quantity), result.status.upper()),
        )
        if trade["state"] == TradeState.WAITING_ADD.value:
            await self.states.transition(trade["trade_id"], TradeState.OPEN)
        await self.database.audit("MOVE_STOP", trade["trade_id"], "SUCCESS", before={"price": local["price"] if local else None},
                                  after={"price": str(new_price)})
        return f"{trade['trade_id']} 止损已更新为 {new_price}"

    async def _cancel_by_state(self, command: TradeCommand, trade: dict, adapter) -> str:
        if trade["state"] == TradeState.PARTIAL_FILL.value:
            raise ValueError("进场单已部分成交；请先确认已创建保护单或使用明确平仓指令，拒绝撤余单")
        order_type = "ENTRY" if trade["state"] in {TradeState.PENDING_ENTRY.value, TradeState.PARTIAL_FILL.value} else "STOP_LOSS"
        local, remote = await self._remote_order(adapter, trade["trade_id"], order_type)
        await adapter.cancel_order(remote.exchange_order_id)
        await self.database.execute("UPDATE orders SET status='CANCELED' WHERE id=?", (local["id"],))
        if order_type == "ENTRY":
            await self.states.transition(trade["trade_id"], TradeState.CANCELLED)
            report = f"{trade['trade_id']} 进场挂单已撤销"
        else:
            await self.states.transition(trade["trade_id"], TradeState.WAITING_ADD)
            report = f"{trade['trade_id']} 止损已取消，交易进入等待补仓/恢复止损状态"
        await self.database.audit("CANCEL_ORDER", trade["trade_id"], "SUCCESS", before={"order_type": order_type})
        return report

    async def _close_position(self, command: TradeCommand, trade: dict, adapter, instrument) -> str:
        if trade["state"] != TradeState.OPEN.value:
            raise ValueError("只有已开仓交易可市价平仓")
        trade_quantity = await self._filled_quantity(trade["trade_id"])
        positions = await adapter.get_positions()
        position = next((item for item in positions if item.instrument_key == trade["instrument_key"]
                         and item.side.value == trade["side"]), None)
        if trade_quantity is None or position is None or position.quantity != trade_quantity:
            raise ValueError("远程仓位不能唯一归属此交易，拒绝市价平仓")
        request = OrderRequest(instrument, command.side, "SELL" if command.side == PositionSide.LONG else "BUY",
                               "MARKET", position.quantity, None, True, self._client_order_id(command, "CLOSE"))
        result = await adapter.close_position(request)
        # 平仓委托已受理后立即撤销同一 trade_id 的保护单，避免其作用于后续新仓。
        protection_rows = await self.database.fetch_all(
            "SELECT id,exchange_order_id,client_order_id FROM orders WHERE trade_id=? "
            "AND order_type IN ('TAKE_PROFIT','STOP_LOSS') AND status IN ('NEW','OPEN','PARTIALLY_FILLED')",
            (trade["trade_id"],),
        )
        remote_ids = {item.client_order_id: item.exchange_order_id for item in await adapter.get_open_orders()}
        for protection in protection_rows:
            remote_id = remote_ids.get(protection["client_order_id"])
            if remote_id is None:
                continue
            await adapter.cancel_order(remote_id)
            await self.database.execute("UPDATE orders SET status='CANCELED' WHERE id=?", (protection["id"],))
        await self.states.transition(trade["trade_id"], TradeState.CLOSING)
        await self.database.audit("CLOSE_POSITION", trade["trade_id"], "SUCCESS", after=result.raw)
        return f"{trade['trade_id']} 已提交市价平仓，订单号 {result.exchange_order_id}"

    async def _filled_quantity(self, trade_id: str) -> Decimal | None:
        """仅使用交易所事件确认的累计成交量，禁止把委托量当作成交量。"""
        rows = await self.database.fetch_all(
            "SELECT filled_quantity FROM orders WHERE trade_id=? AND order_type='ENTRY' ORDER BY id DESC LIMIT 1",
            (trade_id,),
        )
        quantity = Decimal(str(rows[0]["filled_quantity"])) if rows else Decimal("0")
        return quantity if quantity > 0 else None

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
