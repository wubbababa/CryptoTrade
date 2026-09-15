"""确定性仓位计算模块。模型输出不得直接决定下单数量。"""

from __future__ import annotations

from decimal import Decimal

from models import Exchange, TradeCommand
from settings import Settings


class PositionSizingError(ValueError):
    pass


class PositionSizer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def calculate(self, command: TradeCommand, equity: Decimal,
                  leverage_override: Decimal | None = None) -> Decimal:
        if command.entry is None:
            raise PositionSizingError("缺少进场价格，无法计算仓位")
        margin_ratio = Decimal(str(self.settings.raw["trading"]["position_margin_ratio"]))
        leverage = leverage_override or Decimal(str(self.settings.raw["trading"]["leverage"]))
        exchange_limit = Decimal(str(self.settings.exchange_config(command.exchange)["max_leverage"]))
        if not (Decimal("0") < margin_ratio <= Decimal("1")):
            raise PositionSizingError("资金比例必须在 (0, 1] 范围内")
        if leverage <= 0 or leverage > exchange_limit:
            raise PositionSizingError("杠杆无效或超过交易所本地限额")
        notional = equity * margin_ratio * leverage
        return notional / command.entry.reference_price
