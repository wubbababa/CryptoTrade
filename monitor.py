"""启动对账和交易所事件监控。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from decimal import Decimal

from database import Database
from breakeven_strategy import calculate_breakeven, should_trigger, stop_only_improves
from exchange_router import ExchangeRouter
from models import OrderRequest, PositionSide, TradeState

logger = logging.getLogger(__name__)


class Monitor:
    def __init__(self, router: ExchangeRouter, database: Database) -> None:
        self.router = router
        self.database = database

    async def reconcile(self) -> list[str]:
        """以交易所为最终事实来源，刷新仓位快照并报告未关联状态。"""
        warnings: list[str] = []
        # 使用副本遍历，连接或鉴权失败时可安全移除对应适配器。
        for exchange, adapter in list(self.router.adapters.items()):
            try:
                await adapter.validate_account()
                positions = await adapter.get_positions()
                orders = await adapter.get_open_orders()
                for position in positions:
                    await self.database.execute(
                        "INSERT INTO positions(exchange,instrument_key,side,quantity,average_price) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(exchange,instrument_key,side) DO UPDATE SET quantity=excluded.quantity,"
                        "average_price=excluded.average_price,captured_at=CURRENT_TIMESTAMP",
                        (exchange.value, position.instrument_key, position.side.value,
                         str(position.quantity), str(position.average_price)),
                    )
                local = await self.database.fetch_all(
                    "SELECT client_order_id FROM orders WHERE status IN ('NEW','PARTIALLY_FILLED') AND trade_id IN "
                    "(SELECT trade_id FROM trade_instances WHERE exchange=?)", (exchange.value,),
                )
                local_ids = {row["client_order_id"] for row in local}
                remote_ids = {order.client_order_id for order in orders}
                if local_ids != remote_ids:
                    warnings.append(f"{exchange.value} 远程数据和本地数据库挂单状态不一致，已禁止自动推断修改")
            except Exception as exc:
                warnings.append(f"{exchange.value} 对账失败并已禁用：{exc}")
                await adapter.close()
                self.router.adapters.pop(exchange, None)
        return warnings

    async def run_adapter(self, exchange, adapter) -> None:
        try:
            async for event in adapter.stream_market_and_account_events():
                await self.database.execute(
                    "INSERT INTO exchange_events(exchange,event_json) VALUES(?,?)",
                    (exchange.value, json.dumps(event, ensure_ascii=False, default=str)),
                )
                await self.process_event(exchange, adapter, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("%s 事件流断开，实时策略已暂停：%s", exchange.value, exc)

    async def process_event(self, exchange, adapter, event: dict) -> None:
        """处理订单与行情事件。"""
        order_event = self._extract_order_event(exchange.value, event)
        if order_event is None:
            market_event = self._extract_market_event(exchange.value, event)
            if market_event is not None:
                await self._process_breakeven(exchange, adapter, *market_event)
            return
        client_order_id, status = order_event
        rows = await self.database.fetch_all(
            "SELECT o.trade_id,t.state FROM orders o JOIN trade_instances t ON t.trade_id=o.trade_id "
            "WHERE o.client_order_id=? AND o.order_type='ENTRY'",
            (client_order_id,),
        )
        if not rows:
            return
        trade_id, current = rows[0]["trade_id"], rows[0]["state"]
        await self.database.execute("UPDATE orders SET status=? WHERE client_order_id=?", (status, client_order_id))
        if status in {"PARTIALLY_FILLED", "PARTIAL_FILL"}:
            if current == TradeState.PENDING_ENTRY.value:
                await self._set_state(trade_id, TradeState.PARTIAL_FILL)
            return
        if status not in {"FILLED", "FINISHED"}:
            if status in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"} and current in {
                TradeState.PENDING_ENTRY.value, TradeState.PARTIAL_FILL.value,
            }:
                await self._set_state(trade_id, TradeState.CANCELLED)
            return
        try:
            await self._ensure_protection(exchange, adapter, trade_id)
            await self._set_state(trade_id, TradeState.OPEN)
        except Exception as exc:
            await self._set_state(trade_id, TradeState.ERROR_LOCKED, force=True)
            await self.database.audit("PROTECTION_ORDER", trade_id, f"FAILED: {exc}")
            logger.critical("%s 开仓已成交但保护单创建失败，交易已锁定：%s", trade_id, exc)

    @staticmethod
    def _extract_order_event(exchange: str, event: dict) -> tuple[str, str] | None:
        """把三家交易所的订单推送压缩成客户订单号和状态。"""
        if exchange == "OKX":
            if event.get("arg", {}).get("channel") != "orders":
                return None
            data = (event.get("data") or [{}])[0]
            return data.get("clOrdId", ""), str(data.get("state", "")).upper()
        payload = event.get("data", event)
        if exchange == "BINANCE":
            if payload.get("e") != "ORDER_TRADE_UPDATE":
                return None
            order = payload.get("o", {})
            return order.get("c", ""), str(order.get("X", "")).upper()
        if exchange == "GATE" and event.get("channel") == "futures.orders":
            data = (event.get("result") or [{}])[0]
            status = data.get("status", "")
            if status == "finished" and data.get("finish_as") == "filled":
                status = "FILLED"
            return str(data.get("text", "")).removeprefix("t-"), str(status).upper()
        return None

    @staticmethod
    def _extract_market_event(exchange: str, event: dict) -> tuple[str, Decimal] | None:
        """提取统一合约标识与标记价；只接受交易所推送的实时价格字段。"""
        if exchange == "OKX" and event.get("arg", {}).get("channel") == "tickers":
            row = (event.get("data") or [{}])[0]
            price = row.get("last") or row.get("markPx")
            symbol = row.get("instId", "")
        elif exchange == "BINANCE":
            row = event.get("data", event)
            if row.get("e") != "markPriceUpdate":
                return None
            price, symbol = row.get("p"), row.get("s", "")
        elif exchange == "GATE" and event.get("channel") == "futures.tickers":
            row = (event.get("result") or [{}])[0]
            price, symbol = row.get("mark_price") or row.get("last"), row.get("contract", "")
        else:
            return None
        if not price or not symbol:
            return None
        asset = str(symbol).replace("-USDT-SWAP", "").replace("_USDT", "").removesuffix("USDT")
        return f"{asset}/USDT:PERP", Decimal(str(price))

    async def _process_breakeven(self, exchange, adapter, instrument_key: str, market_price: Decimal) -> None:
        """行情到达 50% 目标后，仅向有利方向替换止损并永久标记该笔交易。"""
        trades = await self.database.fetch_all(
            "SELECT trade_id,side FROM trade_instances WHERE exchange=? AND instrument_key=? "
            "AND state='OPEN' AND breakeven_triggered=0",
            (exchange.value, instrument_key),
        )
        if not trades:
            return
        positions = await adapter.get_positions()
        instruments = await adapter.load_instruments()
        for trade in trades:
            try:
                await self._trigger_breakeven(
                    adapter, trade["trade_id"], PositionSide(trade["side"]), instrument_key,
                    market_price, positions, instruments,
                )
            except Exception as exc:
                # 单笔保本失败不得中断同一交易所的其它订单与行情处理。
                await self.database.audit("BREAKEVEN", trade["trade_id"], f"FAILED: {exc}")
                logger.error("%s 自动保本失败：%s", trade["trade_id"], exc)

    async def _trigger_breakeven(self, adapter, trade_id: str, side: PositionSide, instrument_key: str,
                                 market_price: Decimal, positions, instruments) -> None:
        """先建立新止损、再撤销旧止损，尽量避免撤单期间裸仓。"""
        command_rows = await self.database.fetch_all(
            "SELECT payload_json FROM commands WHERE trade_id=? ORDER BY created_at DESC LIMIT 1", (trade_id,)
        )
        stop_rows = await self.database.fetch_all(
            "SELECT id,exchange_order_id,price FROM orders WHERE trade_id=? AND order_type='STOP_LOSS' "
            "AND status IN ('NEW','OPEN','PARTIALLY_FILLED') ORDER BY id DESC LIMIT 1",
            (trade_id,),
        )
        if not command_rows or not stop_rows:
            # 未本地跟踪止损（如旧版 OKX attachAlgoOrds）时拒绝猜测订单号。
            return
        command = json.loads(command_rows[0]["payload_json"])
        targets = command.get("take_profits") or []
        breakeven = command.get("breakeven") or {}
        if not targets:
            return
        position = next(
            (item for item in positions if item.instrument_key == instrument_key and item.side == side), None,
        )
        if position is None or position.quantity <= 0:
            return
        trigger_price, new_stop = calculate_breakeven(
            side, position.average_price, Decimal(str(targets[-1])),
            Decimal(str(breakeven.get("trigger_ratio", "0.50"))),
            Decimal(str(breakeven.get("profit_price_ratio", "0.01"))),
        )
        old_stop = Decimal(str(stop_rows[0]["price"]))
        if not should_trigger(side, market_price, trigger_price) or not stop_only_improves(side, old_stop, new_stop):
            return
        asset = instrument_key.split("/", 1)[0]
        instrument = instruments.get(asset)
        if instrument is None:
            raise ValueError(f"交易所未返回合约 {instrument_key}")
        request = OrderRequest(
            instrument=instrument, position_side=side,
            order_side="SELL" if side == PositionSide.LONG else "BUY", order_type="MARKET",
            quantity=position.quantity, price=new_stop, reduce_only=True,
            client_order_id=self._protection_client_id(trade_id, "BREAKEVEN_STOP"),
        )
        # 新止损成功后才撤旧止损；若撤旧失败，不标记触发，保留双重保护并等待人工处理。
        result = await adapter.place_stop_loss(request)
        try:
            await adapter.cancel_order(stop_rows[0]["exchange_order_id"])
        except Exception:
            await adapter.cancel_order(result.exchange_order_id)
            raise
        await self.database.execute("UPDATE orders SET status='CANCELED' WHERE id=?", (stop_rows[0]["id"],))
        await self.database.execute(
            "INSERT INTO orders(trade_id,exchange_order_id,client_order_id,order_type,price,quantity,status) "
            "VALUES(?,?,?,?,?,?,?)",
            (trade_id, result.exchange_order_id, result.client_order_id, "STOP_LOSS",
             str(new_stop), str(position.quantity), result.status.upper()),
        )
        await self.database.execute(
            "UPDATE trade_instances SET breakeven_triggered=1,updated_at=CURRENT_TIMESTAMP WHERE trade_id=? "
            "AND breakeven_triggered=0",
            (trade_id,),
        )
        await self.database.audit(
            "BREAKEVEN", trade_id, "SUCCESS",
            before={"stop_loss": str(old_stop)},
            after={"market_price": str(market_price), "trigger_price": str(trigger_price), "stop_loss": str(new_stop)},
        )

    async def _ensure_protection(self, exchange, adapter, trade_id: str) -> None:
        """对不支持原子附带保护单的交易所，在完整成交后按真实持仓补建。"""
        if adapter.entry_protection_attached:
            return
        existing = await self.database.fetch_all(
            "SELECT order_type FROM orders WHERE trade_id=? AND order_type IN ('TAKE_PROFIT','STOP_LOSS')",
            (trade_id,),
        )
        existing_types = {row["order_type"] for row in existing}
        trade_rows = await self.database.fetch_all(
            "SELECT instrument_key,side FROM trade_instances WHERE trade_id=?", (trade_id,)
        )
        command_rows = await self.database.fetch_all(
            "SELECT payload_json FROM commands WHERE trade_id=? ORDER BY created_at DESC LIMIT 1", (trade_id,)
        )
        if not trade_rows or not command_rows:
            raise ValueError("缺少交易或原始指令，不能安全创建保护单")
        command = json.loads(command_rows[0]["payload_json"])
        stop_loss, targets = command.get("stop_loss"), command.get("take_profits") or []
        if stop_loss is None or not targets:
            raise ValueError("成交指令缺少止盈或止损，拒绝裸仓")
        instrument_key, side = trade_rows[0]["instrument_key"], PositionSide(trade_rows[0]["side"])
        position = None
        # 成交回报与持仓回报可能乱序；以交易所 REST 的最终状态为准，短暂重查后再决定是否锁定。
        for attempt in range(3):
            positions = await adapter.get_positions()
            position = next(
                (item for item in positions if item.instrument_key == instrument_key and item.side == side), None,
            )
            if position is not None and position.quantity > 0:
                break
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
        if position is None or position.quantity <= 0:
            raise ValueError("未找到与成交订单匹配的真实持仓")
        instruments = await adapter.load_instruments()
        asset = instrument_key.split("/", 1)[0]
        instrument = instruments.get(asset)
        if instrument is None:
            raise ValueError(f"交易所未返回合约 {instrument_key}")
        close_side = "SELL" if side == PositionSide.LONG else "BUY"
        for order_type, price, place in (
            ("TAKE_PROFIT", Decimal(str(targets[-1])), adapter.place_take_profit),
            ("STOP_LOSS", Decimal(str(stop_loss)), adapter.place_stop_loss),
        ):
            if order_type in existing_types:
                continue
            request = OrderRequest(
                instrument=instrument, position_side=side, order_side=close_side, order_type="MARKET",
                quantity=position.quantity, price=price, reduce_only=True,
                client_order_id=self._protection_client_id(trade_id, order_type),
            )
            result = await place(request)
            await self.database.execute(
                "INSERT INTO orders(trade_id,exchange_order_id,client_order_id,order_type,price,quantity,status) "
                "VALUES(?,?,?,?,?,?,?)",
                (trade_id, result.exchange_order_id, result.client_order_id, order_type,
                 str(price), str(position.quantity), result.status.upper()),
            )
            await self.database.audit("PLACE_PROTECTION", trade_id, "SUCCESS", after=result.raw)

    async def _set_state(self, trade_id: str, target: TradeState, force: bool = False) -> None:
        """仅允许正常生命周期转换；保护失败可强制锁定以阻断后续自动操作。"""
        rows = await self.database.fetch_all("SELECT state FROM trade_instances WHERE trade_id=?", (trade_id,))
        if not rows or rows[0]["state"] == target.value:
            return
        current = rows[0]["state"]
        if not force and target == TradeState.OPEN and current not in {
            TradeState.PENDING_ENTRY.value, TradeState.PARTIAL_FILL.value,
        }:
            return
        await self.database.execute(
            "UPDATE trade_instances SET state=?,updated_at=CURRENT_TIMESTAMP WHERE trade_id=?",
            (target.value, trade_id),
        )
        await self.database.audit("STATE_TRANSITION", trade_id, "SUCCESS", current, target.value)

    @staticmethod
    def _protection_client_id(trade_id: str, order_type: str) -> str:
        """生成交易所通用且可重试的保护单客户编号。"""
        digest = hashlib.sha256(f"{trade_id}:{order_type}".encode("utf-8")).hexdigest()
        return "ct" + digest[:26]

    def tasks(self) -> list[asyncio.Task]:
        return [asyncio.create_task(self.run_adapter(exchange, adapter), name=f"monitor-{exchange.value}")
                for exchange, adapter in self.router.adapters.items()]
