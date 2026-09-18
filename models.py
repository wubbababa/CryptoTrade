"""系统统一领域对象。所有金额与数量均使用 Decimal。"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any


class StringEnum(str, Enum):
    """兼容 Python 3.10 的 StrEnum 等价基类。"""

    def __str__(self) -> str:
        return self.value


# 常见资产中文名称及别名映射到标准交易 Ticker 代码
ASSET_ALIASES: dict[str, str] = {
    "黄金": "XAU",
    "GOLD": "XAU",
    "XAUUSD": "XAU",
    "XAUUSDT": "XAU",
    "PAXG": "PAXG",
    "PAXGOLD": "PAXG",
    "XAUT": "XAUT",
    "白银": "XAG",
    "SILVER": "XAG",
    "大饼": "BTC",
    "BITCOIN": "BTC",
    "以太": "ETH",
    "以太坊": "ETH",
    "姨太": "ETH",
    "ETHEREUM": "ETH",
    "索拉纳": "SOL",
    "SOLANA": "SOL",
    "狗币": "DOGE",
    "狗狗币": "DOGE",
    "DOGECOIN": "DOGE",
    "BNB": "BNB",
    "币安币": "BNB",
}


def normalize_asset(asset: str) -> str:
    """标准化资产代码：去除空格、常见后缀并将中文/英文别名映射为标准英文 Ticker 代码。"""
    raw_str = str(asset).strip()
    if not raw_str:
        return ""
    if raw_str in ASSET_ALIASES:
        return ASSET_ALIASES[raw_str]
    cleaned = raw_str.upper()
    if cleaned in ASSET_ALIASES:
        return ASSET_ALIASES[cleaned]
    for suffix in ("/USDT:PERP", "-USDT-SWAP", "_USDT", "-USDT", "/USDT", "USDT", "/USD", "USD"):
        if cleaned.endswith(suffix) and len(cleaned) > len(suffix):
            cleaned = cleaned[:-len(suffix)]
            break
    return ASSET_ALIASES.get(cleaned, cleaned)


class Exchange(StringEnum):
    OKX = "OKX"
    BINANCE = "BINANCE"
    GATE = "GATE"


class CommandType(StringEnum):
    OPEN_POSITION = "OPEN_POSITION"
    AMEND_ENTRY = "AMEND_ENTRY"
    CANCEL_ORDER = "CANCEL_ORDER"
    CLOSE_POSITION = "CLOSE_POSITION"
    MOVE_STOP = "MOVE_STOP"
    AMEND_TAKE_PROFIT = "AMEND_TAKE_PROFIT"
    BREAKEVEN_EXIT = "BREAKEVEN_EXIT"
    ADD_POSITION = "ADD_POSITION"


class PositionSide(StringEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class TradeState(StringEnum):
    RECEIVED = "RECEIVED"
    PENDING_ENTRY = "PENDING_ENTRY"
    PARTIAL_FILL = "PARTIAL_FILL"
    OPEN = "OPEN"
    WAITING_ADD = "WAITING_ADD"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    ERROR_LOCKED = "ERROR_LOCKED"


# 本地订单表中表示「仍在交易所开放/流转」的状态集合（对账与同步共用，避免各处硬编码不一致）。
OPEN_ORDER_STATES = ("NEW", "OPEN", "PARTIALLY_FILLED", "SUBMITTING")


@dataclass(frozen=True, slots=True)
class EntrySpec:
    type: str
    low: Decimal
    high: Decimal

    @property
    def reference_price(self) -> Decimal:
        return (self.low + self.high) / Decimal("2")


@dataclass(frozen=True, slots=True)
class BreakevenSpec:
    trigger_ratio: Decimal = Decimal("0.50")
    profit_price_ratio: Decimal = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class TradeCommand:
    command_id: str
    command_type: CommandType
    exchange: Exchange
    base_asset: str
    side: PositionSide
    entry: EntrySpec | None
    take_profits: tuple[Decimal, ...]
    stop_loss: Decimal | None
    quantity: Decimal | None
    confidence: Decimal
    ambiguities: tuple[str, ...] = ()
    trade_id: str | None = None
    exchange_defaulted: bool = False
    breakeven: BreakevenSpec = field(default_factory=BreakevenSpec)

    @property
    def instrument_key(self) -> str:
        return f"{self.base_asset}/USDT:PERP"


@dataclass(frozen=True, slots=True)
class Instrument:
    instrument_key: str
    exchange_symbol: str
    tick_size: Decimal
    quantity_step: Decimal
    minimum_quantity: Decimal
    minimum_notional: Decimal
    contract_multiplier: Decimal = Decimal("1")


@dataclass(frozen=True, slots=True)
class OrderRequest:
    instrument: Instrument
    position_side: PositionSide
    order_side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None
    reduce_only: bool
    client_order_id: str
    margin_mode: str = "CROSS"
    leverage: Decimal = Decimal("1")
    take_profit_price: Decimal | None = None  # 开仓附带止盈触发价（Mark 市价触发）
    stop_loss_price: Decimal | None = None    # 开仓附带止损触发价（Mark 市价触发）


@dataclass(frozen=True, slots=True)
class OrderResult:
    exchange_order_id: str
    client_order_id: str
    status: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    exchange: Exchange
    instrument_key: str
    side: PositionSide
    quantity: Decimal
    average_price: Decimal
