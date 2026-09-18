"""交易用例编排层：校验、风险、路由、落库和审计。"""

from __future__ import annotations

import asyncio
import json
import hashlib
from dataclasses import asdict, replace
from decimal import Decimal

from database import Database
from exchange_router import ExchangeRouter
from exchanges.base import round_step
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
        """执行指令；未指定交易所的开仓公告会独立广播到默认目标。"""
        if command.command_type == CommandType.OPEN_POSITION and command.exchange_defaulted:
            return await self._execute_default_exchanges(command)
        return await self._execute_single(command)

    async def _execute_default_exchanges(self, command: TradeCommand) -> str:
        """为每个可用默认交易所创建独立子指令，避免跨账户状态相互影响。"""
        targets = self.settings.default_exchanges
        executable: list[TradeCommand] = []
        skipped: list[str] = []
        enabled = set(self.settings.enabled_exchanges())
        for exchange in targets:
            if exchange not in enabled:
                skipped.append(f"{exchange.value}（配置未启用）")
            elif exchange not in self.router.adapters:
                reason = self.router.unavailable_reasons.get(exchange, "适配器未成功初始化")
                skipped.append(f"{exchange.value}（跳过：{reason}）")
            else:
                # 子指令 ID 稳定且含交易所，支持逐交易所幂等和审计追踪。
                executable.append(replace(
                    command,
                    command_id=f"{command.command_id}-{exchange.value}",
                    exchange=exchange,
                    exchange_defaulted=False,
                ))
        if not executable:
            detail = "；".join(skipped) or "没有可用目标"
            raise ValueError(f"默认交易所均不可执行：{detail}")

        results = await asyncio.gather(
            *(self._execute_single(item) for item in executable), return_exceptions=True,
        )
        report_lines = ["公告未指定交易所，已按默认目标独立执行："]
        for item, result in zip(executable, results):
            if isinstance(result, Exception):
                report_lines.append(f"- {item.exchange.value}: 失败：{result}")
            else:
                report_lines.append(f"- {item.exchange.value}: {result}")
        report_lines.extend(f"- {item}" for item in skipped)
        return "\n".join(report_lines)

    async def _execute_single(self, command: TradeCommand) -> str:
        """执行单一交易所指令，保留原有的校验、幂等和状态机行为。"""
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
        quantity = self.position_sizer.calculate(command, equity, leverage)
        # 交易所会把委托数量向下取整到合约步进。本地必须记录同一个「实际提交数量」，
        # 否则重启恢复时拿未取整的委托量对比远程成交持仓必然不一致，自动保本与恢复会被永久拒绝。
        step = instrument.quantity_step
        command = replace(command, quantity=round_step(quantity, step) if step > 0 else quantity)
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
        if command.command_type in {CommandType.AMEND_TAKE_PROFIT, CommandType.BREAKEVEN_EXIT}:
            return await self._amend_take_profit(command, trade, adapter, instrument)
        if command.command_type == CommandType.ADD_POSITION:
            return await self._add_position(command, trade, adapter, instrument)
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

    async def _amend_take_profit(self, command: TradeCommand, trade: dict,
                                 adapter, instrument) -> str:
        """改单止盈；先核对唯一持仓，再先挂新单、后撤旧单，降低裸仓窗口。"""
        if trade["state"] != TradeState.OPEN.value:
            raise ValueError("只有已开仓交易可以修改止盈")
        local, remote = await self._remote_order(adapter, trade["trade_id"], "TAKE_PROFIT")
        positions = await adapter.get_positions()
        position = next((item for item in positions
                         if item.instrument_key == trade["instrument_key"]
                         and item.side.value == trade["side"]), None)
        if position is None or position.quantity <= 0:
            raise ValueError("交易所未找到对应持仓")
        if command.take_profits:
            new_price = command.take_profits[0]
        else:
            # 保本离场：多单在均价上方 1%，空单在均价下方 1% 退出。
            new_price = position.average_price * (
                Decimal("1.01") if command.side == PositionSide.LONG else Decimal("0.99")
            )
        if new_price <= 0:
            raise ValueError("止盈价格必须大于零")
        request = OrderRequest(
            instrument, command.side,
            "SELL" if command.side == PositionSide.LONG else "BUY",
            "MARKET", Decimal(local["quantity"]), new_price, True,
            self._client_order_id(command, "MOVE_TP"),
        )
        result = await adapter.place_take_profit(request)
        try:
            await adapter.cancel_order(remote.exchange_order_id)
        except Exception:
            # 旧单撤销失败时撤回新单，避免同一持仓出现两张止盈单。
            try:
                await adapter.cancel_order(result.exchange_order_id)
            except Exception:
                pass
            raise
        await self.database.execute("UPDATE orders SET status='CANCELED' WHERE id=?", (local["id"],))
        await self.database.execute(
            "INSERT INTO orders(trade_id,exchange_order_id,client_order_id,order_type,price,quantity,status) "
            "VALUES(?,?,?,?,?,?,?)",
            (trade["trade_id"], result.exchange_order_id, result.client_order_id, "TAKE_PROFIT",
             str(new_price), str(request.quantity), result.status.upper()),
        )
        await self.database.audit(
            "AMEND_TAKE_PROFIT", trade["trade_id"], "SUCCESS",
            before={"price": local["price"]},
            after={"price": str(new_price), "order_id": result.exchange_order_id},
        )
        return f"{trade['trade_id']} 止盈已更新为 {new_price}"

    async def _add_position(self, command: TradeCommand, trade: dict,
                            adapter, instrument) -> str:
        """在取消止损后的等待状态挂补仓单，补仓数量沿用当前已成交数量。"""
        if trade["state"] != TradeState.WAITING_ADD.value:
            raise ValueError("只有等待补仓状态可以挂补仓单")
        assert command.entry is not None
        active = await self.database.fetch_all(
            "SELECT exchange_order_id FROM orders WHERE trade_id=? AND order_type='ADD_ENTRY' "
            "AND status IN ('NEW','OPEN','PARTIALLY_FILLED','SUBMITTING') ORDER BY id DESC LIMIT 1",
            (trade["trade_id"],),
        )
        if active:
            raise ValueError("该交易已有活动补仓挂单，请勿重复挂单")
        quantity = await self._filled_quantity(trade["trade_id"])
        if quantity is None or quantity <= 0:
            raise ValueError("缺少当前已成交数量，拒绝猜测补仓数量")
        positions = await adapter.get_positions()
        position = next((item for item in positions
                         if item.instrument_key == trade["instrument_key"]
                         and item.side.value == trade["side"]), None)
        if position is None or position.quantity <= 0:
            raise ValueError("交易所未找到对应持仓")
        price = command.entry.low if command.side == PositionSide.LONG else command.entry.high
        request = OrderRequest(
            instrument, command.side,
            "BUY" if command.side == PositionSide.LONG else "SELL",
            "LIMIT", quantity, price, False,
            self._client_order_id(command, "ADD_ENTRY"),
        )
        result = await adapter.place_entry_order(request)
        await self.database.execute(
            "INSERT INTO orders(trade_id,exchange_order_id,client_order_id,order_type,price,quantity,status) "
            "VALUES(?,?,?,?,?,?,?)",
            (trade["trade_id"], result.exchange_order_id, result.client_order_id, "ADD_ENTRY",
             str(price), str(quantity), result.status.upper()),
        )
        await self.database.audit(
            "ADD_POSITION", trade["trade_id"], "SUCCESS",
            after={"price": str(price), "quantity": str(quantity), "order_id": result.exchange_order_id},
        )
        return f"{trade['trade_id']} 补仓挂单已提交，价格 {price}，数量 {quantity}，订单号 {result.exchange_order_id}"

    async def _cancel_by_state(self, command: TradeCommand, trade: dict, adapter) -> str:
        if trade["state"] == TradeState.PARTIAL_FILL.value:
            raise ValueError("进场单已部分成交；请先确认已创建保护单或使用明确平仓指令，拒绝撤余单")
        order_type = "ENTRY" if trade["state"] in {TradeState.PENDING_ENTRY.value, TradeState.PARTIAL_FILL.value} else "STOP_LOSS"
        local, remote = await self._remote_order(adapter, trade["trade_id"], order_type)
        if order_type == "ENTRY":
            # Binance 未成交进场可能已预挂全平保护单，撤进场前必须先清理，避免留下孤立条件单。
            await self._cancel_pending_protection(trade["trade_id"], adapter)
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

    async def _cancel_pending_protection(self, trade_id: str, adapter) -> None:
        """撤销未成交进场关联的保护单，避免后续仓位被旧条件单误平。

        新流程不再在进场受理阶段预挂保护单；此清理用于兼容历史数据与重启恢复场景。
        """
        rows = await self.database.fetch_all(
            "SELECT id,exchange_order_id FROM orders WHERE trade_id=? AND order_type IN ('TAKE_PROFIT','STOP_LOSS') "
            "AND status IN ('NEW','OPEN','PARTIALLY_FILLED')", (trade_id,),
        )
        for row in rows:
            await adapter.cancel_order(row["exchange_order_id"])
            await self.database.execute("UPDATE orders SET status='CANCELED' WHERE id=?", (row["id"],))

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
        """仅使用成交事件确认的数量，并合并原始进场与补仓成交量。"""
        rows = await self.database.fetch_all(
            "SELECT filled_quantity FROM orders WHERE trade_id=? AND order_type IN ('ENTRY','ADD_ENTRY')",
            (trade_id,),
        )
        quantity = sum((Decimal(str(row["filled_quantity"])) for row in rows), Decimal("0"))
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
        entry_accepted = False
        result = None
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
            entry_accepted = True
            await self.database.execute(
                "UPDATE orders SET exchange_order_id=?,status=? WHERE client_order_id=?",
                (result.exchange_order_id, result.status, client_id),
            )
            # 所有交易所统一采用「成交后再补建保护单」：进场尚未成交时挂条件单会被 Binance 以
            # -4509（Time in Force GTE can only be used with open positions）拒绝，因此不在受理阶段预挂；
            # 保护单改由 Monitor 在收到成交事件后按真实持仓数量创建（见 monitor._ensure_protection）。
            await self.database.execute("UPDATE commands SET trade_id=?,status='EXECUTED' WHERE command_id=?",
                                        (trade_id, command.command_id))
            await self.states.transition(trade_id, TradeState.PENDING_ENTRY)
            await self.database.audit("PLACE_ENTRY", trade_id, "SUCCESS", after=result.raw)
        except Exception as exc:
            if entry_accepted and result is not None:
                # 进场已受理但后续步骤失败时，优先撤掉刚受理的进场，避免留下无保护的潜在仓位；
                # 无论撤单结果如何均锁定人工处理。
                try:
                    await self._cancel_pending_protection(trade_id, adapter)
                    await adapter.cancel_order(result.exchange_order_id)
                    await self.database.execute("UPDATE orders SET status='CANCELED' WHERE client_order_id=?", (client_id,))
                except Exception as cleanup_exc:
                    await self.database.audit("PENDING_PROTECTION_CLEANUP", trade_id, f"FAILED: {cleanup_exc}")
                await self.states.lock(trade_id, f"{command.exchange.value} 进场受理后处理失败：{exc}")
            else:
                await self.database.execute("UPDATE orders SET status='REJECTED' WHERE client_order_id=?", (client_id,))
                await self.states.transition(trade_id, TradeState.REJECTED)
            await self.database.execute("UPDATE commands SET status='REJECTED' WHERE command_id=?", (command.command_id,))
            await self.database.audit("PLACE_ENTRY", trade_id, f"FAILED: {exc}")
            raise
        note = "（公告未指定交易所，已使用本地默认值）" if command.exchange_defaulted else ""
        protection_note = self._protection_note(command, adapter)
        return (
            f"已提交 {trade_id} 进场挂单至 {command.exchange.value}{note}，"
            f"订单号 {result.exchange_order_id}{protection_note}"
        )

    @staticmethod
    def _protection_note(command: TradeCommand, adapter) -> str:
        """在回报中明确止盈止损参数与生效时机，避免把「成交后补建」误判为没有配置。"""
        parts: list[str] = []
        targets = [str(tp) for tp in command.take_profits]
        if targets:
            parts.append("止盈 " + "/".join(targets))
        if command.stop_loss is not None:
            parts.append(f"止损 {command.stop_loss}")
        if not parts:
            return ""
        timing = "已随进场单附带" if adapter.entry_protection_attached else "成交后自动补建"
        return f"，{'，'.join(parts)}（{timing}）"

    @staticmethod
    def _client_order_id(command: TradeCommand, suffix: str) -> str:
        # 保持较短以兼容不同交易所的客户端订单号长度限制。
        # OKX 仅允许字母数字；摘要包含完整消息编号、交易所及订单用途，避免截断碰撞。
        identity = f"{command.exchange.value}:{command.command_id}:{suffix}"
        return "ct" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:26]
