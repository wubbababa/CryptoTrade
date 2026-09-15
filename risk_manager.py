"""交易所独立风险限额检查。"""

from __future__ import annotations

from decimal import Decimal

from models import TradeCommand
from settings import Settings


class RiskError(ValueError):
    pass


class RiskManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def check_open(self, command: TradeCommand, equity: Decimal,
                   current_notional: Decimal = Decimal("0")) -> None:
        if not self.settings.raw["trading"].get("new_positions_enabled", True):
            raise RiskError("全局已禁止新开仓")
        if command.entry is None or command.quantity is None or command.stop_loss is None:
            raise RiskError("风险计算缺少价格、数量或止损")
        cfg = self.settings.exchange_config(command.exchange)
        notional = command.entry.reference_price * command.quantity
        maximum_notional = equity * Decimal(str(cfg["max_position_notional_ratio"]))
        if current_notional + notional > maximum_notional:
            raise RiskError("超过该交易所最大持仓名义价值")
