"""交易所统一适配接口与安全的模拟撮合基类。"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import replace
from decimal import Decimal, ROUND_DOWN
from typing import AsyncIterator
from uuid import uuid4

from models import Instrument, OrderRequest, OrderResult, PositionSnapshot


class ExchangeError(RuntimeError):
    pass


class ExchangeAdapter(ABC):
    async def resolve_leverage(self, instrument: Instrument, requested: Decimal,
                               margin_mode: str) -> Decimal:
        """返回交易所允许的实际杠杆；默认适配器直接采用配置值。"""
        return requested

    async def validate_account(self) -> None:
        """启动时检查账户配置；本地模拟无需远端检查。"""
        return None

    @abstractmethod
    async def get_equity(self) -> Decimal: ...

    @abstractmethod
    async def load_instruments(self) -> dict[str, Instrument]: ...

    @abstractmethod
    async def get_open_orders(self) -> list[OrderResult]: ...

    @abstractmethod
    async def get_positions(self) -> list[PositionSnapshot]: ...

    @abstractmethod
    async def place_entry_order(self, request: OrderRequest) -> OrderResult: ...

    @abstractmethod
    async def amend_entry_order(self, order_id: str, request: OrderRequest) -> OrderResult: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> OrderResult: ...

    @abstractmethod
    async def place_take_profit(self, request: OrderRequest) -> OrderResult: ...

    @abstractmethod
    async def place_stop_loss(self, request: OrderRequest) -> OrderResult: ...

    @abstractmethod
    async def close_position(self, request: OrderRequest) -> OrderResult: ...

    @abstractmethod
    async def stream_market_and_account_events(self) -> AsyncIterator[dict]: ...

    @abstractmethod
    async def close(self) -> None: ...


def round_step(value: Decimal, step: Decimal) -> Decimal:
    """按交易所步进向下取整，避免意外扩大仓位。"""
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


class PaperAdapter(ExchangeAdapter):
    """共享的本地模拟实现；子类只负责各交易所代码和产品规则差异。"""

    def __init__(self, instruments: dict[str, Instrument], equity: Decimal = Decimal("10000")) -> None:
        self.instruments = instruments
        self.equity = equity
        self.orders: dict[str, OrderResult] = {}
        self.positions: list[PositionSnapshot] = []
        self._events: asyncio.Queue[dict] = asyncio.Queue()
        self._closed = False

    async def load_instruments(self) -> dict[str, Instrument]:
        return self.instruments

    async def get_equity(self) -> Decimal:
        return self.equity

    async def get_open_orders(self) -> list[OrderResult]:
        return [order for order in self.orders.values() if order.status in {"NEW", "PARTIALLY_FILLED"}]

    async def get_positions(self) -> list[PositionSnapshot]:
        return list(self.positions)

    def normalize(self, request: OrderRequest) -> OrderRequest:
        instrument = request.instrument
        quantity = round_step(request.quantity, instrument.quantity_step)
        price = round_step(request.price, instrument.tick_size) if request.price is not None else None
        tp = round_step(request.take_profit_price, instrument.tick_size) if request.take_profit_price is not None else None
        sl = round_step(request.stop_loss_price, instrument.tick_size) if request.stop_loss_price is not None else None
        if quantity < instrument.minimum_quantity:
            raise ExchangeError("下单数量低于交易所最小数量")
        if price is not None and quantity * price * instrument.contract_multiplier < instrument.minimum_notional:
            raise ExchangeError("订单名义价值低于交易所最小值")
        return replace(request, quantity=quantity, price=price, take_profit_price=tp, stop_loss_price=sl)

    async def _place(self, request: OrderRequest) -> OrderResult:
        request = self.normalize(request)
        if any(o.client_order_id == request.client_order_id for o in self.orders.values()):
            return next(o for o in self.orders.values() if o.client_order_id == request.client_order_id)
        order_id = "paper-" + uuid4().hex[:20]
        result = OrderResult(order_id, request.client_order_id, "NEW", {
            "symbol": request.instrument.exchange_symbol,
            "price": str(request.price) if request.price is not None else None,
            "quantity": str(request.quantity), "reduce_only": request.reduce_only,
            "margin_mode": request.margin_mode, "leverage": str(request.leverage),
            "take_profit_price": str(request.take_profit_price) if request.take_profit_price is not None else None,
            "stop_loss_price": str(request.stop_loss_price) if request.stop_loss_price is not None else None,
        })
        self.orders[order_id] = result
        await self._events.put({"type": "ORDER", "order_id": order_id, "status": "NEW"})
        return result

    async def place_entry_order(self, request: OrderRequest) -> OrderResult:
        if request.reduce_only:
            raise ExchangeError("进场订单不能为只减仓")
        return await self._place(request)

    async def amend_entry_order(self, order_id: str, request: OrderRequest) -> OrderResult:
        if order_id not in self.orders:
            raise ExchangeError("待修改订单不存在")
        normalized = self.normalize(request)
        result = OrderResult(order_id, normalized.client_order_id, "NEW", {"amended": True})
        self.orders[order_id] = result
        return result

    async def cancel_order(self, order_id: str) -> OrderResult:
        if order_id not in self.orders:
            raise ExchangeError("待撤订单不存在")
        result = replace(self.orders[order_id], status="CANCELED")
        self.orders[order_id] = result
        return result

    async def place_take_profit(self, request: OrderRequest) -> OrderResult:
        if not request.reduce_only:
            raise ExchangeError("止盈订单必须只减仓")
        return await self._place(request)

    async def place_stop_loss(self, request: OrderRequest) -> OrderResult:
        if not request.reduce_only:
            raise ExchangeError("止损订单必须只减仓")
        return await self._place(request)

    async def close_position(self, request: OrderRequest) -> OrderResult:
        if not request.reduce_only:
            raise ExchangeError("平仓订单必须只减仓")
        return await self._place(request)

    async def stream_market_and_account_events(self) -> AsyncIterator[dict]:
        while not self._closed:
            yield await self._events.get()

    async def close(self) -> None:
        self._closed = True
