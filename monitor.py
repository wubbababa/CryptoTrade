"""启动对账和交易所事件监控。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, replace
from decimal import Decimal

from database import Database
from breakeven_strategy import calculate_breakeven, should_trigger, stop_only_improves
from exchange_router import ExchangeRouter
from models import OPEN_ORDER_STATES, OrderRequest, PositionSide, TradeState

logger = logging.getLogger(__name__)


def _decimal(value) -> Decimal | None:
    """将交易所可选数值安全转换为 Decimal。"""
    if value in (None, "", "0", "0.0"):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _first_row(payload) -> dict:
    """取出推送正文的第一行。

    Gate 的订阅回执把 `result` 写成对象（如 `{"status":"success"}`）而不是列表，
    直接按 `payload[0]` 索引会抛 `KeyError(0)`（日志里表现为「事件流断开…：0」），
    并让整个事件流任务退出、该交易所的自动保本永久失效。此处统一做形状归一。
    """
    if isinstance(payload, list):
        return payload[0] if payload and isinstance(payload[0], dict) else {}
    return payload if isinstance(payload, dict) else {}


@dataclass(frozen=True, slots=True)
class OrderEvent:
    """归一后的订单推送。

    `filled_quantity` 一律为标的（基础币）数量：OKX、Gate 以合约张数回报成交量，必须按
    合约面值倍率换算后再落库，否则本地数量会比远程仓位多若干倍，自动保本会被永久拒绝。
    """

    client_order_id: str
    status: str
    filled_quantity: Decimal | None
    average_price: Decimal | None
    symbol: str = ""


class Monitor:
    def __init__(self, router: ExchangeRouter, database: Database, notifier=None) -> None:
        self.router = router
        self.database = database
        self.notifier = notifier
        # 记录最近一次「仓位归属不一致」的签名，避免每个行情推送都重复写审计与告警。
        self._association_reported: dict[tuple[str, str, str], str] = {}

    async def _notify(self, level: str, event: str, message: str, subject_id: str | None = None,
                      details: dict | None = None) -> None:
        if self.notifier is not None:
            await self.notifier.emit(level, event, message, subject_id, details)

    async def reconcile(self) -> list[str]:
        """以交易所为最终事实来源，刷新仓位快照并恢复可唯一关联的本地交易。"""
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
                warnings.extend(await self._recover_positions(exchange, adapter, positions))
                # 恢复过程可能刚补建止盈止损，需重新读取远程挂单后再作一致性比较。
                orders = await adapter.get_open_orders()
                local = await self.database.fetch_all(
                    "SELECT client_order_id FROM orders WHERE UPPER(status) IN ("
                    + ",".join("?" for _ in OPEN_ORDER_STATES) + ") AND trade_id IN "
                    "(SELECT trade_id FROM trade_instances WHERE exchange=?)",
                    (*OPEN_ORDER_STATES, exchange.value),
                )
                local_ids = {row["client_order_id"] for row in local}
                remote_ids = {order.client_order_id for order in orders}
                if local_ids != remote_ids:
                    warnings.append(f"{exchange.value} 远程数据和本地数据库挂单状态不一致，已禁止自动推断修改")
            except Exception as exc:
                warnings.append(f"{exchange.value} 对账失败并已禁用：{exc}")
                await self._notify("CRITICAL", "RECONCILE_FAILED", warnings[-1], details={"exchange": exchange.value})
                await adapter.close()
                self.router.adapters.pop(exchange, None)
        return warnings

    async def _recover_positions(self, exchange, adapter, positions) -> list[str]:
        """恢复重启前已成交但尚未标记为 OPEN 的本地交易。

        交易所仓位是事实来源，但不能仅根据币种猜测一笔交易的归属。只有“交易所、合约、方向”
        能唯一定位到本地待恢复交易时，才补建缺失保护单并恢复自动保本监控。
        """
        warnings: list[str] = []
        for position in positions:
            if position.quantity <= 0:
                continue
            candidates = await self.database.fetch_all(
                "SELECT trade_id,state FROM trade_instances WHERE exchange=? AND instrument_key=? AND side=? "
                "AND state IN ('PENDING_ENTRY','PARTIAL_FILL','OPEN') ORDER BY created_at DESC",
                (exchange.value, position.instrument_key, position.side.value),
            )
            label = f"{exchange.value} {position.instrument_key} {position.side.value}"
            if not candidates:
                warning = f"{label} 存在未关联远程持仓，未恢复自动策略"
                warnings.append(warning)
                await self.database.audit("POSITION_RECOVERY", None, "UNTRACKED", after={
                    "exchange": exchange.value, "instrument_key": position.instrument_key,
                    "side": position.side.value, "quantity": str(position.quantity),
                })
                continue
            trade_ids = [row["trade_id"] for row in candidates]
            open_client_ids = {order.client_order_id for order in await adapter.get_open_orders()}
            entry_rows = await self.database.fetch_all(
                "SELECT client_order_id FROM orders WHERE order_type IN ('ENTRY','ADD_ENTRY') AND trade_id IN ("
                + ",".join("?" for _ in trade_ids) + ")", tuple(trade_ids),
            )
            if {row["client_order_id"] for row in entry_rows} & open_client_ids:
                warning = f"{label} 仍有未成交进场订单，未恢复自动策略"
                warnings.append(warning)
                await self.database.audit("POSITION_RECOVERY", None, "ENTRY_PENDING", after={
                    "exchange": exchange.value, "instrument_key": position.instrument_key,
                    "side": position.side.value, "trade_ids": trade_ids,
                })
                continue
            # 远程未见进场挂单且仓位存在，恢复场景中以该组订单数量作为已成交事实。
            for candidate in candidates:
                await self.database.execute(
                    "UPDATE orders SET status='FILLED',filled_quantity=quantity WHERE trade_id=? AND order_type='ENTRY' "
                    "AND status IN ('NEW','OPEN','PARTIALLY_FILLED')", (candidate["trade_id"],),
                )
            expected_quantity = await self._allocated_quantity(trade_ids)
            if expected_quantity != position.quantity:
                warning = f"{label} 本地交易数量与远程仓位不一致，未恢复自动策略"
                warnings.append(warning)
                await self.database.audit("POSITION_RECOVERY", None, "AMBIGUOUS", after={
                    "exchange": exchange.value, "instrument_key": position.instrument_key,
                    "side": position.side.value, "trade_ids": trade_ids,
                    "expected_quantity": str(expected_quantity), "remote_quantity": str(position.quantity),
                })
                continue
            for candidate in candidates:
                trade_id, state = candidate["trade_id"], candidate["state"]
                try:
                    # 已确认该组数量完整匹配且远程不存在进场挂单，可将遗留的本地进场状态收敛为成交。
                    await self.database.execute(
                        "UPDATE orders SET status='FILLED',filled_quantity=quantity WHERE trade_id=? AND order_type='ENTRY' "
                        "AND status IN ('NEW','OPEN','PARTIALLY_FILLED')", (trade_id,),
                    )
                    # Binance、Gate 等非原子保护单适配器可在重启后补建缺失保护，
                    # OKX 原子保护单则仅恢复本地状态，不猜测其 Algo ID。
                    await self._ensure_protection(exchange, adapter, trade_id)
                    if state != TradeState.OPEN.value:
                        await self._set_state(trade_id, TradeState.OPEN)
                    await self.database.audit("POSITION_RECOVERY", trade_id, "RECOVERED", after={
                        "quantity": str(position.quantity), "average_price": str(position.average_price),
                    })
                except Exception as exc:
                    await self._set_state(trade_id, TradeState.ERROR_LOCKED, force=True)
                    warning = f"{trade_id} 持仓恢复失败且已锁定：{exc}"
                    warnings.append(warning)
                    await self.database.audit("POSITION_RECOVERY", trade_id, f"FAILED: {exc}")
        return warnings

    async def run_adapter(self, exchange, adapter) -> None:
        try:
            async for event in adapter.stream_market_and_account_events():
                # 单条畸形推送不得终止整个事件流：否则该交易所的自动保本会永久失效（见 _first_row）。
                try:
                    # 高频行情仅用于策略判断，不逐条落库；仅保留账户/订单等可审计事件。
                    if self._extract_order_event(exchange.value, event) is not None:
                        await self.database.execute(
                            "INSERT INTO exchange_events(exchange,event_json) VALUES(?,?)",
                            (exchange.value, json.dumps(event, ensure_ascii=False, default=str)),
                        )
                    await self.process_event(exchange, adapter, event)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("%s 事件处理失败，已跳过该条推送：%s", exchange.value, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("%s 事件流断开，实时策略已暂停：%s", exchange.value, exc)

    async def process_event(self, exchange, adapter, event: dict) -> None:
        """处理订单与行情事件。"""
        order_event = await self._parse_order_event(exchange, adapter, event)
        if order_event is None:
            market_event = self._extract_market_event(exchange.value, event)
            if market_event is not None:
                await self._process_breakeven(exchange, adapter, *market_event)
            return
        client_order_id, status = order_event.client_order_id, order_event.status
        filled_quantity, average_price = order_event.filled_quantity, order_event.average_price
        rows = await self.database.fetch_all(
            "SELECT o.trade_id,o.order_type,t.state FROM orders o JOIN trade_instances t ON t.trade_id=o.trade_id "
            "WHERE o.client_order_id=? AND o.order_type IN ('ENTRY','ADD_ENTRY')",
            (client_order_id,),
        )
        if not rows:
            return
        trade_id, order_type, current = rows[0]["trade_id"], rows[0]["order_type"], rows[0]["state"]
        is_add_entry = order_type == "ADD_ENTRY"
        if status in {"FILLED", "FINISHED"} and filled_quantity is None:
            # 完全成交时可安全回填已提交数量；部分成交绝不猜测。
            await self.database.execute(
                "UPDATE orders SET status=?,filled_quantity=quantity WHERE client_order_id=?", (status, client_order_id)
            )
        elif filled_quantity is not None:
            await self.database.execute(
                "UPDATE orders SET status=?,filled_quantity=?,average_fill_price=COALESCE(?,average_fill_price) "
                "WHERE client_order_id=?", (status, str(filled_quantity), str(average_price) if average_price else None, client_order_id)
            )
        else:
            await self.database.execute("UPDATE orders SET status=? WHERE client_order_id=?", (status, client_order_id))
        if status in {"PARTIALLY_FILLED", "PARTIAL_FILL"}:
            if current == TradeState.PENDING_ENTRY.value and not is_add_entry:
                await self._set_state(trade_id, TradeState.PARTIAL_FILL)
            if not is_add_entry:
                try:
                    await self._ensure_protection(exchange, adapter, trade_id)
                except Exception as exc:
                    await self._set_state(trade_id, TradeState.ERROR_LOCKED, force=True)
                    await self._notify("CRITICAL", "PARTIAL_PROTECTION_FAILED", f"部分成交保护单创建失败：{exc}", trade_id)
            return
        if status not in {"FILLED", "FINISHED"}:
            if status in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}:
                # 进场/补仓单被终结：是否安全收敛取决于「是否已产生真实成交」，见 _handle_entry_terminated。
                await self._handle_entry_terminated(exchange, adapter, trade_id, is_add_entry, current)
            return
        try:
            if is_add_entry:
                # 补仓成交后只按当前总数量扩容原止盈；止损由人工「止损/恢复止损」重新设定。
                await self._resize_take_profit(exchange, adapter, trade_id)
                await self._set_state(trade_id, TradeState.OPEN)
            else:
                await self._ensure_protection(exchange, adapter, trade_id)
                await self._set_state(trade_id, TradeState.OPEN)
        except Exception as exc:
            await self._set_state(trade_id, TradeState.ERROR_LOCKED, force=True)
            await self.database.audit("PROTECTION_ORDER", trade_id, f"FAILED: {exc}")
            logger.critical("%s 开仓已成交但保护单创建失败，交易已锁定：%s", trade_id, exc)
            await self._notify("CRITICAL", "PROTECTION_FAILED", f"开仓已成交但保护单创建失败：{exc}", trade_id)

    async def _handle_entry_terminated(self, exchange, adapter, trade_id: str,
                                       is_add_entry: bool, current_state: str) -> None:
        """进场/补仓单被交易所终结（撤单、过期、拒绝）时的安全收敛。

        关键安全边界：**只有零成交才允许撤销保护单并收敛为 CANCELLED**。
        部分成交后交易所撤掉余量（IOC/GTD 到期、风控撤单、人工撤单）时，远程已经
        存在一笔真实仓位；若照旧撤销保护单并标记 CANCELLED，该仓位会永久裸奔——
        状态不在 OPEN，自动保本（只查 OPEN）与重启恢复（只认 PENDING_ENTRY/
        PARTIAL_FILL/OPEN）都不会再接手。

        因此这里一律以「本地成交事实 + 远程持仓」为准，而不是仅看订单终态：
        - 零成交：安全清理预挂保护单并收敛为 CANCELLED（保持原有行为）；
        - 有成交且远程持仓可确认：保留/补建保护单并转入 OPEN，纳入自动保本监控；
        - 有成交但远程持仓无法确认：锁定人工处理，绝不猜测，也绝不撤销保护单。
        """
        # 远程查询失败时异常会向上冒泡，由 run_adapter 逐条跳过该推送；
        # 此路径不会执行任何撤单动作，保护单得以保留，属于安全失败。
        filled_quantity = await self._trade_quantity(trade_id)
        has_fill = filled_quantity is not None and filled_quantity > 0
        # 已有本地成交时，持仓快照可能因推送乱序而短暂缺失，需要重查后再判定；
        # 尚无本地成交时只需一次探测（用于兜住「成交事件丢失但仓位存在」的场景），
        # 避免零成交撤单这类常见路径被无谓地拖慢。
        position = await self._remote_position(adapter, trade_id, retries=3 if has_fill else 1)
        has_position = position is not None and position.quantity > 0

        if not has_fill and not has_position:
            # 零成交：尚未产生任何仓位，撤销预挂保护单并终结交易是安全的。
            if is_add_entry:
                # 补仓单零成交不影响既有持仓，保持 WAITING_ADD 等待人工重新挂单。
                await self.database.audit("ENTRY_TERMINATED", trade_id, "ADD_ENTRY_NO_FILL")
                return
            try:
                await self._cancel_pending_protection(adapter, trade_id)
                await self._set_state(trade_id, TradeState.CANCELLED)
                await self.database.audit("ENTRY_TERMINATED", trade_id, "CANCELLED_NO_FILL")
            except Exception as exc:
                await self._set_state(trade_id, TradeState.ERROR_LOCKED, force=True)
                await self._notify("CRITICAL", "PENDING_PROTECTION_CLEANUP_FAILED",
                                   f"进场撤销后预挂保护单清理失败：{exc}", trade_id)
            return

        if has_fill and not has_position:
            # 本地有成交但远程查不到持仓（持仓推送延迟或归属不明）：
            # 一律锁定人工处理，绝不撤销保护单，绝不凭空按本地数量重挂。
            await self._set_state(trade_id, TradeState.ERROR_LOCKED, force=True)
            await self.database.audit("ENTRY_TERMINATED", trade_id, "FILLED_WITHOUT_POSITION", after={
                "filled_quantity": str(filled_quantity), "is_add_entry": is_add_entry,
            })
            await self._notify("CRITICAL", "ENTRY_TERMINATED_FILLED_WITHOUT_POSITION",
                               f"订单已终结但本地有成交 {filled_quantity}、远程未确认持仓；"
                               "已保留保护单并锁定人工处理", trade_id)
            return

        # 有真实成交：走与「完全成交」相同的收敛路径，保留/补建保护单后纳入自动保本监控。
        try:
            if is_add_entry:
                # 补仓部分成交：按合并后的真实数量扩容原止盈，止损仍由人工指令重设。
                await self._resize_take_profit(exchange, adapter, trade_id)
            else:
                await self._ensure_protection(exchange, adapter, trade_id)
            await self._set_state(trade_id, TradeState.OPEN)
            await self.database.audit("ENTRY_TERMINATED", trade_id, "KEPT_PROTECTED",
                                      before={"state": current_state}, after={
                                          "state": TradeState.OPEN.value,
                                          "filled_quantity": str(filled_quantity) if filled_quantity is not None else None,
                                          "remote_quantity": str(position.quantity),
                                      })
            await self._notify("WARNING", "ENTRY_TERMINATED_WITH_FILL",
                               f"进场单余量已被交易所终结，但已成交 {position.quantity}；"
                               "已保留保护单并转入 OPEN，自动保本继续生效", trade_id)
        except Exception as exc:
            await self._set_state(trade_id, TradeState.ERROR_LOCKED, force=True)
            await self.database.audit("ENTRY_TERMINATED", trade_id, f"FAILED: {exc}")
            await self._notify("CRITICAL", "ENTRY_TERMINATED_PROTECTION_FAILED",
                               f"订单终结后保护单收敛失败，交易已锁定：{exc}", trade_id)

    async def _remote_position(self, adapter, trade_id: str, retries: int = 3):
        """按本地交易记录定位远程汇总持仓；交易不存在或远程无该持仓时返回 None。

        订单终结推送与持仓回报可能乱序（撤单事件常先于仓位快照更新到达），而本方法的
        结果会决定「保留保护单」还是「锁定交易」，因此默认与 `_ensure_protection`
        一样做短暂重查，避免把仅仅延迟的持仓误判为不存在。
        """
        rows = await self.database.fetch_all(
            "SELECT instrument_key,side FROM trade_instances WHERE trade_id=?", (trade_id,)
        )
        if not rows:
            return None
        instrument_key = rows[0]["instrument_key"]
        side = PositionSide(rows[0]["side"])
        position = None
        for attempt in range(max(retries, 1)):
            positions = await adapter.get_positions()
            position = next(
                (item for item in positions if item.instrument_key == instrument_key and item.side == side), None,
            )
            if position is not None and position.quantity > 0:
                return position
            if attempt < retries - 1:
                await asyncio.sleep(0.5 * (attempt + 1))
        return position

    async def _parse_order_event(self, exchange, adapter, event: dict) -> OrderEvent | None:
        """解析订单推送，并把交易所回报的成交量统一换算为标的（基础币）数量。"""
        parsed = self._extract_order_event(exchange.value, event)
        # 订阅回执等推送没有合约字段，也就没有可换算的成交量，交给后续查库自然忽略。
        if parsed is None or parsed.filled_quantity is None or not parsed.symbol:
            return parsed
        base_quantity = await adapter.to_base_quantity(parsed.symbol, parsed.filled_quantity)
        return replace(parsed, filled_quantity=base_quantity)

    @staticmethod
    def _extract_order_event(exchange: str, event: dict) -> OrderEvent | None:
        """把三家交易所的订单推送压缩成客户订单号、状态、成交量与合约代码。

        此处保留交易所原始计量单位（OKX/Gate 为合约张数），由 `_parse_order_event` 结合
        适配器的合约面值倍率换算成标的数量，避免换算规则在多处重复维护。
        """
        if exchange == "OKX":
            if event.get("arg", {}).get("channel") != "orders":
                return None
            data = _first_row(event.get("data"))
            return OrderEvent(str(data.get("clOrdId", "")), str(data.get("state", "")).upper(),
                              _decimal(data.get("accFillSz")), _decimal(data.get("avgPx")),
                              str(data.get("instId", "")))
        payload = event.get("data", event)
        if exchange == "BINANCE":
            if payload.get("e") != "ORDER_TRADE_UPDATE":
                return None
            order = payload.get("o", {})
            return OrderEvent(str(order.get("c", "")), str(order.get("X", "")).upper(),
                              _decimal(order.get("z")), _decimal(order.get("ap")),
                              str(order.get("s", "")))
        if exchange == "GATE" and event.get("channel") == "futures.orders":
            data = _first_row(event.get("result"))
            status = data.get("status", "")
            if status == "finished" and data.get("finish_as") == "filled":
                status = "FILLED"
            size = _decimal(data.get("size"))
            left = _decimal(data.get("left"))
            filled = abs(size - left) if size is not None and left is not None else None
            return OrderEvent(str(data.get("text", "")).removeprefix("t-"), str(status).upper(),
                              filled, _decimal(data.get("fill_price")), str(data.get("contract", "")))
        return None

    @staticmethod
    def _extract_market_event(exchange: str, event: dict) -> tuple[str, Decimal] | None:
        """提取统一合约标识与标记价；只接受交易所推送的实时价格字段。"""
        if exchange == "OKX" and event.get("arg", {}).get("channel") == "tickers":
            row = _first_row(event.get("data"))
            price = row.get("last") or row.get("markPx")
            symbol = row.get("instId", "")
        elif exchange == "BINANCE":
            row = event.get("data", event)
            if row.get("e") != "markPriceUpdate":
                return None
            price, symbol = row.get("p"), row.get("s", "")
        elif exchange == "GATE" and event.get("channel") == "futures.tickers":
            row = _first_row(event.get("result"))
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
        # 同一合约同方向在交易所通常是汇总仓位。只有本地每笔进场数量之和与远程仓位
        # 完全一致时，才可将保护单和保本动作精确归属到各 trade_id。
        sides = {PositionSide(trade["side"]) for trade in trades}
        if len(sides) != 1:
            return
        side = sides.pop()
        position = next(
            (item for item in positions if item.instrument_key == instrument_key and item.side == side), None,
        )
        if position is None or position.quantity <= 0:
            return
        expected_quantity = await self._allocated_quantity([trade["trade_id"] for trade in trades])
        association_key = (exchange.value, instrument_key, side.value)
        if expected_quantity != position.quantity:
            reason = (f"远程仓位={position.quantity}，本地已关联交易数量={expected_quantity}；"
                      "拒绝对汇总仓位执行自动保本")
            # 行情推送每秒可达十余条，同一处不一致只记录一次，避免审计表被同一原因刷爆。
            signature = f"{position.quantity}/{expected_quantity}"
            if self._association_reported.get(association_key) != signature:
                self._association_reported[association_key] = signature
                for trade in trades:
                    await self.database.audit("POSITION_ASSOCIATION", trade["trade_id"], "MISMATCH", after={
                        "instrument_key": instrument_key, "side": side.value, "reason": reason,
                    })
                logger.warning("%s %s", instrument_key, reason)
            return
        # 数量重新对齐后清除记录，便于后续再次出现不一致时重新告警。
        self._association_reported.pop(association_key, None)
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
                await self._notify("ERROR", "BREAKEVEN_FAILED", f"自动保本失败：{exc}", trade["trade_id"])

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
        trade_quantity = await self._trade_quantity(trade_id)
        if trade_quantity is None or trade_quantity <= 0:
            raise ValueError("缺少该交易的进场数量，无法关联汇总仓位")
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
            quantity=trade_quantity, price=new_stop, reduce_only=True,
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
             str(new_stop), str(trade_quantity), result.status.upper()),
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
        await self._notify("INFO", "BREAKEVEN_MOVED", f"自动保本已将止损移动至 {new_stop}", trade_id,
                           {"market_price": str(market_price), "trigger_price": str(trigger_price)})

    async def _ensure_protection(self, exchange, adapter, trade_id: str) -> None:
        """对不支持原子附带保护单的交易所，在完整成交后按真实持仓补建。"""
        if adapter.entry_protection_attached:
            return
        existing = await self.database.fetch_all(
            "SELECT id,order_type,exchange_order_id,client_order_id,quantity,status FROM orders WHERE trade_id=? "
            "AND order_type IN ('TAKE_PROFIT','STOP_LOSS') AND status IN ('NEW','OPEN','PARTIALLY_FILLED')",
            (trade_id,),
        )
        # 重启恢复时必须确认本地记录的保护单仍在交易所开放列表中；
        # 不使用同一客户端编号盲目重建一个可能已经被交易所受理的订单。
        remote_client_ids = {order.client_order_id for order in await adapter.get_open_orders()}
        missing_remote = [row["order_type"] for row in existing if row["client_order_id"] not in remote_client_ids]
        if missing_remote:
            raise ValueError(f"本地保护单 {','.join(missing_remote)} 未出现在交易所开放订单中")
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
        trade_quantity = await self._trade_quantity(trade_id)
        if trade_quantity is None or trade_quantity <= 0:
            raise ValueError("缺少该交易的进场数量，不能创建保护单")
        if position.quantity < trade_quantity:
            raise ValueError(
                f"远程持仓数量 {position.quantity} 小于该交易进场数量 {trade_quantity}，归属不一致"
            )
        # 部分成交继续扩大时，旧保护量不足；先撤旧保护单再用新的唯一客户编号重建。
        undersized = [row for row in existing if Decimal(str(row["quantity"])) != trade_quantity]
        for row in undersized:
            await adapter.cancel_order(row["exchange_order_id"])
            await self.database.execute("UPDATE orders SET status='CANCELED' WHERE id=?", (row["id"],))
        if undersized:
            existing_types = {row["order_type"] for row in existing if row not in undersized}
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
                quantity=trade_quantity, price=price, reduce_only=True,
                client_order_id=self._protection_client_id(trade_id, f"{order_type}:{trade_quantity}"),
            )
            result = await place(request)
            await self.database.execute(
                "INSERT INTO orders(trade_id,exchange_order_id,client_order_id,order_type,price,quantity,status) "
                "VALUES(?,?,?,?,?,?,?)",
                (trade_id, result.exchange_order_id, result.client_order_id, order_type,
                 str(price), str(trade_quantity), result.status.upper()),
            )
            await self.database.audit("PLACE_PROTECTION", trade_id, "SUCCESS", after=result.raw)

    async def _resize_take_profit(self, exchange, adapter, trade_id: str) -> None:
        """补仓成交后按合并仓位数量重建原止盈，止损留给人工重新设定。"""
        rows = await self.database.fetch_all(
            "SELECT id,exchange_order_id,client_order_id,price FROM orders "
            "WHERE trade_id=? AND order_type='TAKE_PROFIT' AND status IN ('NEW','OPEN','PARTIALLY_FILLED') "
            "ORDER BY id DESC LIMIT 1", (trade_id,),
        )
        if not rows:
            raise ValueError("未找到活动止盈单，拒绝在补仓后猜测保护价格")
        local = rows[0]
        remote = next((item for item in await adapter.get_open_orders()
                       if item.client_order_id == local["client_order_id"]), None)
        if remote is None:
            raise ValueError("活动止盈单未在交易所开放订单中，拒绝猜测")
        trade_rows = await self.database.fetch_all(
            "SELECT instrument_key,side FROM trade_instances WHERE trade_id=?", (trade_id,)
        )
        if not trade_rows:
            raise ValueError("交易不存在，无法重建补仓后的止盈")
        instrument_key, side = trade_rows[0]["instrument_key"], PositionSide(trade_rows[0]["side"])
        quantity = await self._trade_quantity(trade_id)
        if quantity is None or quantity <= 0:
            raise ValueError("缺少补仓后的合并成交数量")
        positions = await adapter.get_positions()
        position = next((item for item in positions
                         if item.instrument_key == instrument_key and item.side == side), None)
        if position is None or position.quantity != quantity:
            raise ValueError("补仓成交数量与交易所汇总持仓不一致，拒绝重建止盈")
        instruments = await adapter.load_instruments()
        asset = instrument_key.split("/", 1)[0]
        instrument = instruments.get(asset)
        if instrument is None:
            raise ValueError(f"交易所未返回合约 {instrument_key}")
        request = OrderRequest(
            instrument=instrument, position_side=side,
            order_side="SELL" if side == PositionSide.LONG else "BUY", order_type="MARKET",
            quantity=quantity, price=Decimal(str(local["price"])), reduce_only=True,
            client_order_id=self._protection_client_id(trade_id, f"ADD_TAKE_PROFIT:{quantity}"),
        )
        result = await adapter.place_take_profit(request)
        try:
            await adapter.cancel_order(remote.exchange_order_id)
        except Exception:
            try:
                await adapter.cancel_order(result.exchange_order_id)
            except Exception:
                pass
            raise
        await self.database.execute("UPDATE orders SET status='CANCELED' WHERE id=?", (local["id"],))
        await self.database.execute(
            "INSERT INTO orders(trade_id,exchange_order_id,client_order_id,order_type,price,quantity,status) "
            "VALUES(?,?,?,?,?,?,?)",
            (trade_id, result.exchange_order_id, result.client_order_id, "TAKE_PROFIT",
             str(local["price"]), str(quantity), result.status.upper()),
        )
        await self.database.audit(
            "RESIZE_TAKE_PROFIT", trade_id, "SUCCESS",
            before={"quantity": str(local.get("quantity", "")), "price": local["price"]},
            after={"quantity": str(quantity), "price": local["price"]},
        )

    async def _cancel_pending_protection(self, adapter, trade_id: str) -> None:
        """进场撤销时清理 Binance 等交易所预挂的保护单。"""
        rows = await self.database.fetch_all(
            "SELECT id,exchange_order_id FROM orders WHERE trade_id=? AND order_type IN ('TAKE_PROFIT','STOP_LOSS') "
            "AND status IN ('NEW','OPEN','PARTIALLY_FILLED')", (trade_id,),
        )
        for row in rows:
            await adapter.cancel_order(row["exchange_order_id"])
            await self.database.execute("UPDATE orders SET status='CANCELED' WHERE id=?", (row["id"],))

    async def _trade_quantity(self, trade_id: str) -> Decimal | None:
        """读取单笔交易的进场数量，作为远程汇总仓位的唯一分配依据。"""
        rows = await self.database.fetch_all(
            "SELECT filled_quantity FROM orders WHERE trade_id=? AND order_type IN ('ENTRY','ADD_ENTRY')",
            (trade_id,),
        )
        quantity = sum((Decimal(str(row["filled_quantity"])) for row in rows), Decimal("0"))
        return quantity if quantity > 0 else None

    async def _allocated_quantity(self, trade_ids: list[str]) -> Decimal:
        """汇总一组已打开交易的进场数量；缺少记录时返回不可能匹配的负值。"""
        total = Decimal("0")
        for trade_id in trade_ids:
            quantity = await self._trade_quantity(trade_id)
            if quantity is None:
                return Decimal("-1")
            total += quantity
        return total

    async def _set_state(self, trade_id: str, target: TradeState, force: bool = False) -> None:
        """仅允许正常生命周期转换；保护失败可强制锁定以阻断后续自动操作。"""
        rows = await self.database.fetch_all("SELECT state FROM trade_instances WHERE trade_id=?", (trade_id,))
        if not rows or rows[0]["state"] == target.value:
            return
        current = rows[0]["state"]
        if not force and target == TradeState.OPEN and current not in {
            TradeState.PENDING_ENTRY.value, TradeState.PARTIAL_FILL.value, TradeState.WAITING_ADD.value,
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
